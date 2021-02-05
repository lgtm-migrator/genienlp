#
# Copyright (c) 2018, Salesforce, Inc.
#                     The Board of Trustees of the Leland Stanford Junior University
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


import asyncio
from genienlp.calibrate import ConfidenceEstimator
import json
import logging
import sys
import os
from pprint import pformat

import torch

from . import models
from .data_utils.example import Example, NumericalizedExamples
from .tasks.registry import get_tasks
from .util import set_seed, init_devices, load_config_json, log_model_size
from .validate import generate_with_model

logger = logging.getLogger(__name__)


class Server:
    def __init__(self, args, numericalizer, model, device, confidence_estimators, estimator_filenames):
        self.args = args
        self.device = device
        self.numericalizer = numericalizer
        self.model = model
        self.confidence_estimators = confidence_estimators
        self.estimator_filenames = estimator_filenames

        self._cached_tasks = dict()

    def numericalize_examples(self, ex):
        all_features = NumericalizedExamples.from_examples(ex, self.numericalizer)
        # make a single batch with all examples
        return NumericalizedExamples.collate_batches(all_features, self.numericalizer, device=self.device)

    def handle_request(self, line):
        with torch.no_grad():
            if isinstance(line, dict):
                request = line
            else:
                request = json.loads(line)

            task_name = request['task'] if 'task' in request else 'generic'
            if task_name in self._cached_tasks:
                task = self._cached_tasks[task_name]
            else:
                task = list(get_tasks([task_name], self.args).values())[0]
                self._cached_tasks[task_name] = task

            if 'instances' in request:
                examples = []
                # request['instances'] is an array of {context, question, answer, example_id}
                for instance in request['instances']:
                    example_id, context, question, answer = instance.get('example_id', ''), instance['context'], instance['question'], instance.get('answer', '')
                    if not context:
                        context = task.default_context
                    if not question:
                        question = task.default_question

                    ex = Example.from_raw(str(example_id), context, question, answer, preprocess=task.preprocess_field, lower=self.args.lower)
                    examples.append(ex)

                self.model.add_new_vocab_from_data([task])
                batch = self.numericalize_examples(examples)
                # it is a single batch, so wrap it in []
                if self.args.calibrator_paths is not None:
                    output = generate_with_model(self.model, [batch], self.numericalizer, task, self.args,
                                                    output_predictions_only=True,
                                                    confidence_estimators=self.confidence_estimators)
                    output_instances = []
                    for idx, p in enumerate(output.predictions):
                        output_instance = {'answer': p[0], 'score': {}}
                        for e_idx, estimator_scores in enumerate(output.confidence_scores):
                            output_instance['score'][self.estimator_filenames[e_idx]] = float(estimator_scores[idx])
                        output_instances.append(output_instance)


                    response = json.dumps({ 'id': request['id'], 'instances': output_instances})
                else:
                    output = generate_with_model(self.model, [batch], self.numericalizer, task, self.args,
                                                    output_predictions_only=True)

                    response = json.dumps({ 'id': request['id'], 'instances': [{ 'answer': p[0]} for p in output.predictions]})
                return response + '\n'
            else:
                context = request['context']
                if not context:
                    context = task.default_context
                question = request['question']
                if not question:
                    question = task.default_question
                answer = ''

                ex = Example.from_raw(str(request['id']), context, question, answer, preprocess=task.preprocess_field, lower=self.args.lower)

                self.model.add_new_vocab_from_data([task])
                batch = self.numericalize_examples([ex])
                if self.args.calibrator_paths is not None:
                    output = generate_with_model(self.model, [batch], self.numericalizer, task, self.args,
                                                    output_predictions_only=True,
                                                    confidence_estimators=self.confidence_estimators)
                    response = json.dumps(dict(id=request['id'], answer=output.predictions[0][0], score=dict(zip(self.estimator_filenames, [float(s) for s in output.confidence_scores]))))
                else:
                    output = generate_with_model(self.model, [batch], self.numericalizer, task, self.args,
                                                    output_predictions_only=True)
                    response = json.dumps(dict(id=request['id'], answer=output.predictions[0][0]))
                return response + '\n'

    async def handle_client(self, client_reader, client_writer):
        try:
            line = await client_reader.readline()
            while line:
                client_writer.write(self.handle_request(line).encode('utf-8'))
                line = await client_reader.readline()

        except IOError:
            logger.info('Connection to client_reader closed')
            try:
                client_writer.close()
            except IOError:
                pass

    def _run_tcp(self):
        loop = asyncio.get_event_loop()
        server = loop.run_until_complete(asyncio.start_server(self.handle_client, port=self.args.port))
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            pass
        server.close()
        loop.run_until_complete(server.wait_closed())
        loop.close()

    def _run_stdin(self):
        try:
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                sys.stdout.write(self.handle_request(line))
                sys.stdout.flush()
        except KeyboardInterrupt:
            pass

    def run(self):
        log_model_size(logger, self.model, self.args.model)
        self.model.to(self.device)

        self.model.eval()
        if self.args.stdin:
            self._run_stdin()
        else:
            self._run_tcp()


def parse_argv(parser):
    parser.add_argument('--path', type=str, required=True)
    parser.add_argument('--devices', default=[0], nargs='+', type=int,
                        help='a list of devices that can be used (multi-gpu currently WIP)')
    parser.add_argument('--seed', default=123, type=int, help='Random seed.')
    parser.add_argument('--embeddings', default='.embeddings', type=str, help='where to save embeddings.')
    parser.add_argument('--checkpoint_name', default='best.pth',
                        help='Checkpoint file to use (relative to --path, defaults to best.pth)')
    parser.add_argument('--port', default=8401, type=int, help='TCP port to listen on')
    parser.add_argument('--stdin', action='store_true', help='Interact on stdin/stdout instead of TCP')
    parser.add_argument('--locale', default='en', help='locale tag of the language to parse')
    parser.add_argument('--inference_name', default='nlp', help='name used by kfserving inference service, alphanumeric only')

    # for confidence estimation:
    parser.add_argument('--calibrator_paths', type=str, nargs='+', default=None,
                        help='If provided, will be used to output confidence scores for each prediction. Defaults to `--path`/calibrator.pkl')

def init(args):
    load_config_json(args)
    set_seed(args)

    logger.info(f'Arguments:\n{pformat(vars(args))}')
    logger.info(f'Loading from {args.best_checkpoint}')

    devices = init_devices(args)
    device = devices[0] # server only runs on a single device

    Model = getattr(models, args.model)
    model, _ = Model.from_pretrained(args.path,
                                     model_checkpoint_file=args.checkpoint_name,
                                     args=args,
                                     device=device
                                    )

    model.set_decoder_start_token_id(args.locale)

    
    model.to(device)
    model.eval()

    # set the default path for calibrator if it exists
    estimator_filenames = []
    if args.calibrator_paths is None:
        for filename in os.listdir(args.path):
            if filename.endswith('.pkl'):
                path = os.path.join(args.path, filename)
                if not ConfidenceEstimator.is_estimator(path):
                    continue
                if args.calibrator_paths is None:
                    args.calibrator_paths = []
                args.calibrator_paths.append(path)
                estimator_filenames.append(os.path.splitext(filename)[0])

    confidence_estimators = None
    if args.calibrator_paths is not None:
        confidence_estimators = []
        for path in args.calibrator_paths:
            estimator = ConfidenceEstimator.load(path)
            confidence_estimators.append(estimator)
            logger.info('Loading confidence estimator "%s" from %s', estimator.name, path)
        args.mc_dropout_num = confidence_estimators[0].mc_dropout_num # we assume all estimators have the same mc_dropout_num

    return model, device, confidence_estimators, estimator_filenames

def main(args):
    model, device, confidence_estimators, estimator_filenames = init(args)
    server = Server(args, model.numericalizer, model, device, confidence_estimators, estimator_filenames)
    server.run()
