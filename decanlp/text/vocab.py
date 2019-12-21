from __future__ import unicode_literals
import array
from collections import defaultdict, Counter
import io
import logging
import os
import zipfile
import numpy as np
import gzip
import shutil

import six
from six.moves.urllib.request import urlretrieve
import torch
from tqdm import tqdm
import tarfile

from .utils import reporthook

logger = logging.getLogger(__name__)

MAX_WORD_LENGTH = 100


class Vocab(object):
    """Defines a vocabulary object that will be used to numericalize a field.

    Attributes:
        freqs: A collections.Counter object holding the frequencies of tokens
            in the data used to build the Vocab.
        stoi: A collections.defaultdict instance mapping token strings to
            numerical identifiers.
        itos: A list of token strings indexed by their numerical identifiers.
    """
    def __init__(self, counter, max_size=None, min_freq=1, specials=('<pad>',),
                 vectors=None, cat_vectors=True):
        """Create a Vocab object from a collections.Counter.

        Arguments:
            counter: collections.Counter object holding the frequencies of
                each value found in the data.
            max_size: The maximum size of the vocabulary, or None for no
                maximum. Default: None.
            min_freq: The minimum frequency needed to include a token in the
                vocabulary. Values less than 1 will be set to 1. Default: 1.
            specials: The list of special tokens (e.g., padding or eos) that
                will be prepended to the vocabulary in addition to an <unk>
                token. Default: ['<pad>']
            vectors: One of either the available pretrained vectors
                or custom pretrained vectors (see Vocab.load_vectors);
                or a list of aforementioned vectors
        """
        self.freqs = counter
        counter = counter.copy()
        min_freq = max(min_freq, 1)
        counter.update(specials)

        self.stoi = defaultdict(_default_unk_index)
        self.stoi.update({tok: i for i, tok in enumerate(specials)})
        self.itos = list(specials)

        counter.subtract({tok: counter[tok] for tok in specials})
        max_size = None if max_size is None else max_size + len(self.itos)

        # sort by frequency, then alphabetically
        words_and_frequencies = sorted(counter.items(), key=lambda tup: tup[0])
        words_and_frequencies.sort(key=lambda tup: tup[1], reverse=True)

        for word, freq in words_and_frequencies:
            if freq < min_freq or len(self.itos) == max_size:
                break
            self.itos.append(word)
            self.stoi[word] = len(self.itos) - 1

        self.vectors = None
        if vectors is not None:
            self.load_vectors(vectors, cat=cat_vectors)

    def __eq__(self, other):
        if self.freqs != other.freqs:
            return False
        if self.stoi != other.stoi:
            return False
        if self.itos != other.itos:
            return False
        if self.vectors != other.vectors:
            return False
        return True

    def __len__(self):
        return len(self.itos)

    def extend(self, v, sort=False):
        words = sorted(v.itos) if sort else v.itos
        for w in words:
            if w not in self.stoi:
                self.itos.append(w)
                self.stoi[w] = len(self.itos) - 1

    def load_vectors(self, vectors, cat=True):
        """
        Arguments:
            vectors: one of or a list containing instantiations of the
                GloVe, CharNGram, or Vectors classes. Alternatively, one
                of or a list of available pretrained vectors:
                    charngram.100d
                    fasttext.en.300d
                    fasttext.simple.300d
                    glove.42B.300d
                    glove.840B.300d
                    glove.twitter.27B.25d
                    glove.twitter.27B.50d
                    glove.twitter.27B.100d
                    glove.twitter.27B.200d
                    glove.6B.50d
                    glove.6B.100d
                    glove.6B.200d
                    glove.6B.300d
        """
        if not isinstance(vectors, list):
            vectors = [vectors]
        for idx, vector in enumerate(vectors):
            if six.PY2 and isinstance(vector, str):
                vector = six.text_type(vector)
            if isinstance(vector, six.string_types):
                # Convert the string pretrained vector identifier
                # to a Vectors object
                if vector not in pretrained_aliases:
                    raise ValueError(
                        "Got string input vector {}, but allowed pretrained "
                        "vectors are {}".format(
                            vector, list(pretrained_aliases.keys())))
                vectors[idx] = pretrained_aliases[vector]()
            elif not isinstance(vector, Vectors):
                raise ValueError(
                    "Got input vectors of type {}, expected str or "
                    "Vectors object".format(type(vector)))

        if cat:
            tot_dim = sum(v.dim for v in vectors)
            self.vectors = torch.Tensor(len(self), tot_dim)
            for ti, token in enumerate(self.itos):
                start_dim = 0
                for v in vectors:
                    end_dim = start_dim + v.dim
                    self.vectors[ti][start_dim:end_dim] = v[token.strip()]
                    start_dim = end_dim
                assert(start_dim == tot_dim)
        else:
            self.vectors = [torch.Tensor(len(self), v.dim) for v in vectors]
            for ti, t in enumerate(self.itos):
                for vi, v in enumerate(vectors):
                    self.vectors[vi][ti] = v[t.strip()]

    def set_vectors(self, stoi, vectors, dim, unk_init=torch.Tensor.zero_):
        """
        Set the vectors for the Vocab instance from a collection of Tensors.

        Arguments:
            stoi: A dictionary of string to the index of the associated vector
                in the `vectors` input argument.
            vectors: An indexed iterable (or other structure supporting __getitem__) that
                given an input index, returns a FloatTensor representing the vector
                for the token associated with the index. For example,
                vector[stoi["string"]] should return the vector for "string".
            dim: The dimensionality of the vectors.
            unk_init (callback): by default, initialize out-of-vocabulary word vectors
                to zero vectors; can be any function that takes in a Tensor and
                returns a Tensor of the same size. Default: torch.Tensor.zero_
        """
        self.vectors = torch.Tensor(len(self), dim)
        for i, token in enumerate(self.itos):
            wv_index = stoi.get(token, None)
            if wv_index is not None:
                self.vectors[i] = vectors[wv_index]
            else:
                self.vectors[i] = unk_init(self.vectors[i])

    @staticmethod
    def build_from_data(field_names, *args, unk_token=None, pad_token=None, init_token=None, eos_token=None, **kwargs):
        """Construct the Vocab object for this field from one or more datasets.

        Arguments:
            Positional arguments: Dataset objects or other iterable data
                sources from which to construct the Vocab object that
                represents the set of possible values for this field. If
                a Dataset object is provided, all columns corresponding
                to this field are used; individual columns can also be
                provided directly.
            Remaining keyword arguments: Passed to the constructor of Vocab.
        """
        counter = Counter()
        sources = []
        for arg in args:
            sources += [getattr(ex, name) for name in field_names for ex in arg]
        for data in sources:
            for x in data:
                counter.update(x)
        specials = [unk_token, pad_token, init_token, eos_token]
        specials = [tok for tok in specials if tok is not None]
        return Vocab(counter, specials=specials, **kwargs)


def string_hash(x):
    """ Simple deterministic string hash

    Based on https://cp-algorithms.com/string/string-hashing.html.
    We need this because str.__hash__ is not deterministic (it varies with each process restart)
    and it uses 8 bytes (which is too much for our uses)
    """

    P = 1009
    h = 0
    for c in x:
        h = (h << 10) + h + ord(c) * P
        h = h & 0xFFFFFFFF
    return np.uint32(h)


class HashTable(object):
    EMPTY_BUCKET = 0

    def __init__(self, itos, table=None):
        # open addressing hashing, with load factor 0.50

        if table is not None:
            assert isinstance(itos, np.ndarray)

            self.itos = itos
            self.table = table
            self.table_size = table.shape[0]
        else:

            max_str_len = max(len(x) for x in itos)
            self.itos = np.array(itos, dtype='U' + str(max_str_len))

            self.table_size = int(len(itos) * 2)
            self.table = np.zeros((self.table_size,), dtype=np.int64)

            self._build(itos)

    def _build(self, itos):
        for i, word in enumerate(tqdm(itos, total=len(itos))):
            hash = string_hash(word)
            bucket = hash % self.table_size

            while self.table[bucket] != self.EMPTY_BUCKET:
                hash += 7
                bucket = hash % self.table_size

            self.itos[i] = word
            self.table[bucket] = 1 + i

    def __iter__(self):
        return iter(self.itos)
    def __reversed__(self):
        return reversed(self.itos)

    def __len__(self):
        return self.itos

    def __eq__(self, other):
        return isinstance(other, HashTable) and self.itos == other.itos
    def __hash__(self):
        return hash(self.itos)

    def _find(self, key):
        hash = string_hash(key)
        for probe_count in range(self.table_size):
            bucket = (hash + 7 * probe_count) % self.table_size

            key_index = self.table[bucket]
            if key_index == self.EMPTY_BUCKET:
                return None

            if self.itos[key_index - 1] == key:
                return key_index - 1
        return None

    def __getitem__(self, key):
        found = self._find(key)
        if found is None:
            raise KeyError(f'Invalid key {key}')
        else:
            return found

    def __contains__(self, key):
        found = self._find(key)
        return found is not None

    def get(self, key, default=None):
        found = self._find(key)
        if found is None:
            return default
        else:
            return found


class Vectors(object):

    def __init__(self, name, cache='.vector_cache',
                 url=None, unk_init=torch.Tensor.zero_):
        """Arguments:
               name: name of the file that contains the vectors
               cache: directory for cached vectors
               url: url for download if vectors not found in cache
               unk_init (callback): by default, initalize out-of-vocabulary word vectors
                   to zero vectors; can be any function that takes in a Tensor and
                   returns a Tensor of the same size
         """
        self.unk_init = unk_init
        self.cache(name, cache, url=url)

    def __getitem__(self, token):
        if token in self.stoi:
            return self.vectors[self.stoi[token]]
        else:
            return self.unk_init(torch.Tensor(1, self.dim))

    def cache(self, name, cache, url=None):
        if os.path.isfile(name):
            path = name
            path_vectors_np = os.path.join(cache, os.path.basename(name)) + '.vectors.npy'
            path_itos_np = os.path.join(cache, os.path.basename(name)) + '.itos.npy'
            path_table_np = os.path.join(cache, os.path.basename(name)) + '.table.npy'
        else:
            path = os.path.join(cache, name)
            path_vectors_np = path + '.vectors.npy'
            path_itos_np = path + '.itos.npy'
            path_table_np = path + '.table.npy'

        if not os.path.isfile(path_vectors_np):
            if not os.path.isfile(path) and url:
                logger.info('Downloading vectors from {}'.format(url))
                if not os.path.exists(cache):
                    os.makedirs(cache)
                dest = os.path.join(cache, os.path.basename(url))
                if not os.path.isfile(dest):
                    with tqdm(unit='B', unit_scale=True, miniters=1, desc=dest) as t:
                        urlretrieve(url, dest, reporthook=reporthook(t))
                logger.info('Extracting vectors into {}'.format(cache))
                ext = os.path.splitext(dest)[1][1:]
                if ext == 'zip':
                    with zipfile.ZipFile(dest, "r") as zf:
                        zf.extractall(cache)
                elif dest.endswith('.tar.gz'):
                    with tarfile.open(dest, 'r:gz') as tar:
                        tar.extractall(path=cache)
                elif ext == 'gz':
                    with gzip.open(dest, 'rb') as fin, open(path, 'wb') as fout:
                        shutil.copyfileobj(fin, fout)
            if not os.path.isfile(path):
                raise RuntimeError('no vectors found at {}'.format(path))

            # str call is necessary for Python 2/3 compatibility, since
            # argument must be Python 2 str (Python 3 bytes) or
            # Python 3 str (Python 2 unicode)
            itos, vectors, dim = [], array.array(str('d')), None

            # Try to read the whole file with utf-8 encoding.
            binary_lines = False
            try:
                with io.open(path, encoding="utf8") as f:
                    lines = [line for line in f]
            # If there are malformed lines, read in binary mode
            # and manually decode each word from utf-8
            except:
                logger.warning("Could not read {} as UTF8 file, "
                               "reading file as bytes and skipping "
                               "words with malformed UTF8.".format(path))
                with open(path, 'rb') as f:
                    lines = [line for line in f]
                binary_lines = True

            logger.info("Loading vectors from {}".format(path))
            vectors = None
            i = 0
            for line in tqdm(lines, total=len(lines)):
                # Explicitly splitting on " " is important, so we don't
                # get rid of Unicode non-breaking spaces in the vectors.
                entries = line.rstrip().split(b" " if binary_lines else " ")

                word, entries = entries[0], entries[1:]
                if dim is None and len(entries) > 1:
                    dim = len(entries)
                    vectors = np.zeros((len(lines), dim), dtype=np.float32)
                elif len(entries) == 1:
                    logger.warning("Skipping token {} with 1-dimensional "
                                   "vector {}; likely a header".format(word, entries))
                    continue
                elif dim != len(entries):
                    raise RuntimeError(
                        "Vector for token {} has {} dimensions, but previously "
                        "read vectors have {} dimensions. All vectors must have "
                        "the same number of dimensions.".format(word, len(entries), dim))

                if binary_lines:
                    try:
                        if isinstance(word, six.binary_type):
                            word = word.decode('utf-8')
                    except:
                        logger.info("Skipping non-UTF8 token {}".format(repr(word)))
                        continue

                if len(word) > MAX_WORD_LENGTH:
                    continue
                vectors[i] = [float(x) for x in entries]
                i += 1
                itos.append(word)
            del lines

            # we dropped some words because they were too long, so now vectors
            # has some empty entries at the end
            assert len(itos) <= vectors.shape[0]
            vectors = vectors[:len(itos)]

            self.stoi = HashTable(itos)
            self.itos = self.stoi.itos
            del itos
            assert self.itos.shape[0] == vectors.shape[0]

            print('Saving vectors to {}'.format(path_vectors_np))

            np.save(path_vectors_np, vectors)
            np.save(path_itos_np, self.itos)
            np.save(path_table_np, self.stoi.table)

            self.vectors = torch.from_numpy(vectors)
            self.dim = dim
        else:
            logger.info('Loading vectors from {}'.format(path_vectors_np))

            vectors = np.load(path_vectors_np, mmap_mode='r')
            itos = np.load(path_itos_np, mmap_mode='r')
            table = np.load(path_table_np, mmap_mode='r')
            self.stoi = HashTable(itos, table)
            self.itos = self.stoi.itos
            self.vectors = torch.from_numpy(vectors)
            self.dim = self.vectors.size()[1]


class GloVe(Vectors):
    url = {
        '42B': 'http://nlp.stanford.edu/data/glove.42B.300d.zip',
        '840B': 'http://nlp.stanford.edu/data/glove.840B.300d.zip',
        'twitter.27B': 'http://nlp.stanford.edu/data/glove.twitter.27B.zip',
        '6B': 'http://nlp.stanford.edu/data/glove.6B.zip',
    }

    def __init__(self, name='840B', dim=300, **kwargs):
        url = self.url[name]
        name = 'glove.{}.{}d.txt'.format(name, str(dim))
        super(GloVe, self).__init__(name, url=url, **kwargs)


class FastText(Vectors):

    url_base = 'https://dl.fbaipublicfiles.com/fasttext/vectors-crawl/cc.{}.300.vec.gz'

    def __init__(self, language="en", **kwargs):
        url = self.url_base.format(language)
        name = os.path.basename(url)[:-3]
        super(FastText, self).__init__(name, url=url, **kwargs)


class CharNGram(Vectors):

    name = 'charNgram.txt'
    url = ('http://www.logos.t.u-tokyo.ac.jp/~hassy/publications/arxiv2016jmt/'
           'jmt_pre-trained_embeddings.tar.gz')

    def __init__(self, **kwargs):
        super(CharNGram, self).__init__(self.name, url=self.url, **kwargs)

    def __getitem__(self, token):
        vector = torch.Tensor(1, self.dim).zero_()
        if token == "<unk>":
            return self.unk_init(vector)
        # These literals need to be coerced to unicode for Python 2 compatibility
        # when we try to join them with read ngrams from the files.
        chars = ['#BEGIN#'] + list(token) + ['#END#']
        num_vectors = 0
        for n in [2, 3, 4]:
            end = len(chars) - n + 1
            grams = [chars[i:(i + n)] for i in range(end)]
            for gram in grams:
                gram_key = '{}gram-{}'.format(n, ''.join(gram))
                if gram_key in self.stoi:
                    vector += self.vectors[self.stoi[gram_key]]
                    num_vectors += 1
        if num_vectors > 0:
            vector /= num_vectors
        else:
            vector = self.unk_init(vector)
        return vector


def _default_unk_index():
    return 0


pretrained_aliases = {
    "charngram.100d": lambda: CharNGram(),
    "fasttext.en.300d": lambda: FastText(language="en"),
    "fasttext.simple.300d": lambda: FastText(language="simple"),
    "glove.42B.300d": lambda: GloVe(name="42B", dim="300"),
    "glove.840B.300d": lambda: GloVe(name="840B", dim="300"),
    "glove.twitter.27B.25d": lambda: GloVe(name="twitter.27B", dim="25"),
    "glove.twitter.27B.50d": lambda: GloVe(name="twitter.27B", dim="50"),
    "glove.twitter.27B.100d": lambda: GloVe(name="twitter.27B", dim="100"),
    "glove.twitter.27B.200d": lambda: GloVe(name="twitter.27B", dim="200"),
    "glove.6B.50d": lambda: GloVe(name="6B", dim="50"),
    "glove.6B.100d": lambda: GloVe(name="6B", dim="100"),
    "glove.6B.200d": lambda: GloVe(name="6B", dim="200"),
    "glove.6B.300d": lambda: GloVe(name="6B", dim="300")
}
