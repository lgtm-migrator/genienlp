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


import torch
from .util import pad
from .metrics import compute_metrics


def compute_validation_outputs(model, val_iter, field, iteration):
    loss, predictions, answers, contexts, questions = [], [], [], [], []
    for batch_idx, batch in enumerate(val_iter):
        l, p = model(batch, iteration)
        loss.append(l)
        predictions.append(pad(p, 150, dim=-1, val=field.vocab.stoi[field.pad_token]))
        a = pad(batch.answer.value.data.cpu(), 150, dim=-1, val=field.vocab.stoi[field.pad_token])
        answers.append(a)
        c = pad(batch.context.value.data.cpu(), 150, dim=-1, val=field.vocab.stoi[field.pad_token])
        contexts.append(c)
        q = pad(batch.question.value.data.cpu(), 150, dim=-1, val=field.vocab.stoi[field.pad_token])
        questions.append(q)

    loss = torch.cat(loss, 0) if loss[0] is not None else None
    predictions = torch.cat(predictions, 0)
    answers = torch.cat(answers, 0)
    contexts = torch.cat(contexts, 0)
    questions = torch.cat(questions, 0)
    return loss, predictions, answers, contexts, questions


def all_reverse(tensor, world_size, task, field, field_name, dim=0):
    
    if world_size > 1:
        tensor = tensor.float() # tensors must be on cpu and float for all_gather
        all_tensors = [torch.zeros_like(tensor) for _ in range(world_size)]
        torch.distributed.barrier() # all_gather is experimental for gloo, found that these barriers were necessary
        torch.distributed.all_gather(all_tensors, tensor)
        torch.distributed.barrier()
        tensor = torch.cat(all_tensors, 0).long() # tensors must be long for reverse

    return field.reverse(tensor, detokenize=task.detokenize, field_name=field_name)


def gather_results(model, val_iter, field, world_size, task, iteration):
    loss, predictions, answers, contexts, questions = compute_validation_outputs(model, val_iter, field, iteration)
    answers = all_reverse(answers, world_size, task, field, field_name='answer')
    predictions = all_reverse(predictions, world_size, task, field, field_name='answer')
    contexts = all_reverse(contexts, world_size, task, field, field_name='context')
    questions = all_reverse(questions, world_size, task, field, field_name='question')

    return loss, predictions, answers, contexts, questions


def print_results(keys, values, rank=None, num_print=1):
    print()
    start = rank * num_print if rank is not None else 0
    end = start + num_print
    values = [val[start:end] for val in values]
    for ex_idx in range(len(values[0])):
        for key_idx, key in enumerate(keys):
            value = values[key_idx][ex_idx]
            v = value[0] if isinstance(value, list) else value
            print(f'{key}: {repr(v)}')
        print()


def validate(task, val_iter, model, logger, field, world_size, rank, iteration, num_print=10, args=None):
    with torch.no_grad():
        model.eval()
        names = ['greedy', 'answer', 'context', 'question']
        loss, predictions, answers, contexts, questions = gather_results(model, val_iter, field, world_size, task, iteration)
        predictions = [p.replace('UNK', 'OOV') for p in predictions]

        metrics, answers = compute_metrics(predictions, answers, task.metrics, args=args)
        results = [predictions, answers, contexts, questions]
        print_results(names, results, rank=rank, num_print=num_print)

        return loss, metrics
