"""
Bonito train
"""

import os
import re
from glob import glob
from functools import partial
from time import perf_counter
from collections import OrderedDict
from datetime import datetime

from bonito.util import accuracy, decode_ref, permute, concat, match_names
import bonito

import torch
import numpy as np
import torch.nn as nn
from tqdm import tqdm
from torch.optim.lr_scheduler import LambdaLR
import torch.cuda.amp as amp

class ChunkDataSet:
    def __init__(self, chunks, targets, lengths):
        self.chunks = np.expand_dims(chunks, axis=1)
        self.targets = targets
        self.lengths = lengths

    def __getitem__(self, i):
        return (
            self.chunks[i].astype(np.float32),
            self.targets[i].astype(np.int64),
            self.lengths[i].astype(np.int64),
        )

    def __len__(self):
        return len(self.lengths)


def const_schedule(y):
    """
    Constant Scheduler
    """
    return lambda t: y


def linear_schedule(y0, y1):
    """
    Linear Scheduler
    """
    return lambda t: y0 + (y1 - y0) * t


def cosine_decay_schedule(y0, y1):
    """
    Cosine Decay Scheduler
    """
    return lambda t: y1 + 0.5 * (y0 - y1) * (np.cos(t * np.pi) + 1.0)


def piecewise_schedule(knots, funcs):
    """
    Piecewise Scheduler
    """
    def f(t):
        i = np.searchsorted(knots, t)
        t0 = 0.0 if i == 0 else knots[i - 1]
        t1 = 1.0 if i == len(knots) else knots[i]
        return funcs[i]((t - t0) / (t1 - t0))
    return f


def func_scheduler(optimizer, func, total_steps, warmup_steps=None, warmup_ratio=0.1, start_step=0):
    """
    Learning Rate Scheduler
    """
    if warmup_steps:
        y0 = func(0.0)
        func = piecewise_schedule(
            [warmup_steps / total_steps],
            [linear_schedule(warmup_ratio * y0, y0), func]
        )
    return LambdaLR(optimizer, (lambda step: func((step + start_step) / total_steps)))


def load_state(dirname, device, model):
    """
    Load a model state dict from disk
    """
    model.to(device)

    weight_no = None

    weight_files = glob(os.path.join(dirname, "weights_*.tar"))
    if weight_files:
        weight_no = max([int(re.sub(".*_([0-9]+).tar", "\\1", w)) for w in weight_files])

    if weight_no:
        print("[picking up from epoch %s]" % weight_no)
        state_dict = torch.load(
            os.path.join(dirname, 'weights_%s.tar' % weight_no), map_location=device
        )
        state_dict = {k2: state_dict[k1] for k1, k2 in match_names(state_dict, model).items()}
        new_state_dict = OrderedDict()
        for k, v in state_dict.items():
            name = k.replace('module.', '')
            new_state_dict[name] = v

        model.load_state_dict(new_state_dict)
        epoch = weight_no
    else:
        epoch = 0

    return epoch


class Trainer:
    def __init__(self, model, device, train_loader, valid_loader, criterion=None, use_amp=True):
        self.model = model.to(device)
        self.device = device
        self.train_loader = train_loader
        self.valid_loader = valid_loader
        self.criterion = criterion or (model.seqdist.ctc_loss if hasattr(model, 'seqdist') else model.ctc_label_smoothing_loss)
        self.use_amp = use_amp
        self.scaler = torch.cuda.amp.GradScaler(enabled=use_amp)
        self.optimizer = None

    def train_one_step(self, batch):
        data, targets, lengths = batch

        self.optimizer.zero_grad()
        with amp.autocast(enabled=self.use_amp):
            scores = self.model(data.to(self.device))
            losses = self.criterion(scores, targets.to(self.device), lengths.to(self.device))

        if not isinstance(losses, dict):
            losses = {'loss': losses}

        self.scaler.scale(losses['loss']).backward()
        self.scaler.unscale_(self.optimizer)
        grad_norm = torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=2.0).item()
        self.scaler.step(self.optimizer)
        self.scaler.update()

        return losses, grad_norm

    def train_one_epoch(self, loss_log, lr_scheduler):
        t0 = perf_counter()
        chunks = 0
        self.model.train()

        progress_bar = tqdm(
            total=len(self.train_loader), desc='[0/{}]'.format(len(self.train_loader.dataset)),
            ascii=True, leave=True, ncols=100, bar_format='{l_bar}{bar}| [{elapsed}{postfix}]'
        )
        smoothed_loss = None

        with progress_bar:

            for batch in self.train_loader:

                chunks += batch[0].shape[0]

                losses, grad_norm = self.train_one_step(batch)
                losses = {k: v.item() for k,v in losses.items()}

                if lr_scheduler is not None: lr_scheduler.step()

                smoothed_loss = losses['loss'] if smoothed_loss is None else (0.01 * losses['loss'] + 0.99 * smoothed_loss)

                progress_bar.set_postfix(loss='%.4f' % smoothed_loss)
                progress_bar.set_description("[{}/{}]".format(chunks, len(self.train_loader.dataset)))
                progress_bar.update()

                if loss_log is not None:
                    loss_log.append({'chunks': chunks, 'time': perf_counter() - t0, 'grad_norm': grad_norm, **losses})

        return smoothed_loss, perf_counter() - t0

    def validate_one_step(self, batch):
        data, targets, lengths = batch

        scores = self.model(data.to(self.device))
        losses = self.criterion(scores, targets.to(self.device), lengths.to(self.device))
        losses = {k: v.item() for k, v in losses.items()} if isinstance(losses, dict) else losses.item()
        if hasattr(self.model, 'decode_batch'):
            seqs = self.model.decode_batch(scores)
        else:
            seqs = [self.model.decode(x) for x in permute(scores, 'TNC', 'NTC')]
        refs = [decode_ref(target, self.model.alphabet) for target in targets]
        accs = [
            accuracy(ref, seq, min_coverage=0.5) if len(seq) else 0. for ref, seq in zip(refs, seqs)
        ]
        return seqs, refs, accs, losses

    def validate_one_epoch(self):
        self.model.eval()
        with torch.no_grad():
            seqs, refs, accs, losses = zip(*(self.validate_one_step(batch) for batch in self.valid_loader))
        seqs, refs, accs = (sum(x, []) for x in (seqs, refs, accs))
        loss = np.mean([(x['ctc_loss'] if isinstance(x, dict) else x) for x in losses])
        return loss, np.mean(accs), np.median(accs)

    def init_optimizer(self, lr, **kwargs):
        self.optimizer = torch.optim.AdamW(self.model.parameters(), lr=lr, **kwargs)

    def get_lr_scheduler(self, epochs, last_epoch=0):
        return func_scheduler(
            self.optimizer, cosine_decay_schedule(1.0, 0.1), epochs * len(self.train_loader),
            warmup_steps=500,
            start_step=last_epoch*len(self.train_loader)
        )

    def fit(self, workdir, epochs=1, lr=2e-3, last_epoch=0):
        if self.optimizer is None:
            self.init_optimizer(lr)

        lr_scheduler = self.get_lr_scheduler(epochs, last_epoch=last_epoch)

        for epoch in range(1 + last_epoch, epochs + 1 + last_epoch):
            try:
                with bonito.io.CSVLogger(os.path.join(workdir, 'losses_{}.csv'.format(epoch))) as loss_log:
                    train_loss, duration = self.train_one_epoch(loss_log, lr_scheduler)

                model_state = self.model.module.state_dict() if hasattr(self.model, 'module') else self.model.state_dict()
                torch.save(model_state, os.path.join(workdir, "weights_%s.tar" % epoch))

                val_loss, val_mean, val_median = self.validate_one_epoch()
            except KeyboardInterrupt:
                break

            print("[epoch {}] directory={} loss={:.4f} mean_acc={:.3f}% median_acc={:.3f}%".format(
                epoch, workdir, val_loss, val_mean, val_median
            ))

            with bonito.io.CSVLogger(os.path.join(workdir, 'training.csv')) as training_log:
                training_log.append({
                    'time': datetime.today(),
                    'duration': int(duration),
                    'epoch': epoch,
                    'train_loss': train_loss,
                    'validation_loss': val_loss,
                    'validation_mean': val_mean,
                    'validation_median': val_median
                })