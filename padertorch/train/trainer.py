import contextlib
import itertools
import os
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import padertorch as pt
import torch
from padertorch.configurable import Configurable
from paderbox.utils.nested import flatten, nested_op, nested_update
from padertorch.train.optimizer import Optimizer, Adam
from padertorch.train.run_time_tests import test_run
from padertorch.train.hooks import *
from tensorboardX import SummaryWriter

from padertorch.train.trigger import IntervalTrigger

__all__ = [
    'Trainer',
]


class ContextTimerDict:
    """
    To be able to keep the measurements, we need to create the object before.
    Then each measurement can be started with a context manager.

    >>> np.set_printoptions(precision=2)
    >>> timer = ContextTimerDict()
    >>> with timer['test']:
    ...     time.sleep(0.1)
    >>> with timer['test']:
    ...     time.sleep(0.1)
    >>> with timer['test_2']:
    ...     time.sleep(0.1)

    Ignore timing, when an exception is raised
    >>> with contextlib.suppress(Exception), timer['test_2']:
    ...     raise Exception
    >>> timer
    ContextTimerDict: {'test': array([0.1, 0.1]), 'test_2': array([0.1])}
    >>> timer.as_dict
    {'test': array([0.1, 0.1]), 'test_2': array([0.1])}

    """
    def __init__(self):
        self.timings = defaultdict(list)
        self.timestamp = time.perf_counter  # time.process_time

    @contextlib.contextmanager
    def __getitem__(self, item):
        assert isinstance(item, str), item
        start = self.timestamp()
        yield
        end = self.timestamp()
        self.timings[item].append(end - start)

    @property
    def as_dict(self):
        return {k: np.array(time) for k, time in self.timings.items()}

    def __repr__(self):
        return f'{self.__class__.__name__}: ' + repr(self.as_dict)

    def __str__(self):
        return str(self.as_dict)


class Trainer(Configurable):

    @classmethod
    def get_signature(cls):
        default_dict = super().get_signature()
        default_dict['optimizer'] = {'cls': Adam}
        return default_dict

    def __init__(
            self,
            model,
            storage_dir,
            optimizer=None,
            loss_weights=None,
            summary_step=(1, 'epoch'),
            checkpoint_step=(1, 'epoch'),
            validate_step=(1, 'epoch'),
            max_step=(1, 'epoch'),
            gpu=0 if torch.cuda.is_available() else None,
            init_checkpoint=None,
            seed=0,
    ):
        self.model = model
        self.use_cuda = gpu is not None
        self.gpu_device = None
        if self.use_cuda:
            self.gpu_device = int(gpu)
            self.model = nested_op(
                lambda m: m.cuda(self.gpu_device), self.model
            )
        else:
            self.gpu_device = None
        self.optimizer = optimizer

        nested_op(
            lambda model, opti: opti.set_parameters(model.parameters())
            if opti is not None else None,
            self.model, self.optimizer
        )

        self.storage_dir = Path(storage_dir).expanduser().absolute()
        self.reset_timer()
        self.iteration = 0
        self.epoch = 0
        if init_checkpoint is not None:
            self.load_checkpoint(
                Path(init_checkpoint).expanduser().absolute(),
            )
        self.seed = seed

        self.summary_step = summary_step
        self.checkpoint_trigger = IntervalTrigger.new(checkpoint_step)
        self.validate_step = validate_step
        self.max_step = max_step

        self.loss_weights = loss_weights

    def reset_timer(self):
        self.timer = ContextTimerDict()

    def test_run(self, train_iterator, validation_iterator):
        """
        Run a test on the trainer instance (i.e. model test).

        Tests:
         - forward (train and validate)
         - deterministic output in eval
         - simple review dict test

        """
        test_run(
            self,
            train_iterator,
            validation_iterator,
        )

    def train(self, train_iterator, validation_iterator, hooks=None):
        os.makedirs(str(self.storage_dir / 'checkpoints'), exist_ok=True)

        torch.backends.cudnn.enabled = True
        torch.backends.cudnn.benchmark = False

        torch.manual_seed(self.seed)
        torch.cuda.manual_seed(self.seed)

        # Change model to train mode (e.g. activate dropout)
        nested_op(lambda m: m.train(), self.model)

        hooks, summary_hook = self.get_hooks(hooks, validation_iterator)
        # For training continue set the correct last value
        for hook in hooks:
            hook.trigger.set_last(self.iteration, self.epoch)
        self.checkpoint_trigger.set_last(self.iteration, self.epoch)

        # ================ MAIN TRAINING LOOP! ===================
        try:
            for self.epoch in itertools.count(self.epoch):  # infinite loop
                data_iterator = iter(train_iterator)
                for self.iteration in itertools.count(self.iteration):
                    # Because of last validation, validation must be before
                    # "max_iterations".
                    for hook in hooks:
                        hook.pre_function(self)
                    if self.checkpoint_trigger(
                            iteration=self.iteration, epoch=self.epoch
                    ) or self.iteration == 1:
                        self.save_checkpoint()
                    with self.timer['time_per_step']:
                        try:
                            with self.timer['time_per_data_loading']:
                                example = next(data_iterator)
                        except StopIteration:
                            if self.iteration > 0:
                                break
                            else:
                                raise Exception('Zero length train iterator')

                        example = pt.data.batch_to_device(
                            example, self.use_cuda, self.gpu_device
                        )
                        # Todo: backup OOM
                        with self.timer['time_per_train_step']:
                            model_output, review = self.train_step(example)
                        for hook in hooks:
                            hook.post_function(
                                self, example, model_output, review
                            )

        except StopTraining:
            pass
        finally:
            summary_hook.dump_summary(self)
            self.save_checkpoint()

    def train_step(self, example):
        msg = 'Overwrite the train_step and validation_step, ' \
              'when you have multiple models.'
        assert isinstance(self.model, torch.nn.Module), (self.model, msg)
        assert isinstance(self.optimizer, Optimizer), (self.optimizer, msg)
        self.optimizer.zero_grad()
        model_out = self.model(example)
        review = self.model.review(example, model_out)
        self.backward(review)
        grad_summary = self.clip_grad()
        self.optimizer.step()
        nested_update(review, grad_summary)
        return model_out, review

    def validate(self, validation_iterator):
        train_end_time = self.timer.timestamp()

        if hasattr(self, '_start_non_validation_time'):
            self.timer.timings['non_validation_time'].append(
                train_end_time - self._start_non_validation_time
            )

        with self.timer['validation_time'], torch.no_grad():
            # Change model to eval mode (e.g. deactivate dropout)
            nested_op(lambda m: m.eval(), self.model)
            try:
                for i, example in enumerate(validation_iterator):
                    example = pt.data.batch_to_device(
                        example, self.use_cuda, self.gpu_device
                    )
                    yield self.validation_step(example)
            finally:
                nested_op(lambda m: m.train(), self.model)
                self._start_non_validation_time = self.timer.timestamp()

    def validation_step(self, example):
        assert isinstance(self.model, torch.nn.Module), (
            self.model, 'Overwrite the train_step and validation_step, when you have multiple models.'
        )
        model_out = self.model(example)
        return self.model.review(example, model_out)

    def backward(self, review, retain_graph=False):
        loss = 0.
        loss_weights = self.loss_weights
        if loss_weights is None and len(review['losses']) != 1:
            raise Exception(
                'You can not have multiple losses without specifying '
                f'loss_weights. losses: {review["losses"]}'
            )
        for key, value in review['losses'].items():
            weight = loss_weights[key] if loss_weights is not None else 1.
            loss += weight * value
        loss.backward(retain_graph=retain_graph)

    def get_hooks(self, hooks, validation_iterator):
        if hooks is None:
            hooks = []
        hooks = pt.utils.to_list(hooks)
        summary_hook = SummaryHook(self.summary_step, self.validate_step)
        hooks.append(summary_hook)
        hooks.append(ValidationHook(self.validate_step, validation_iterator))
        hooks.append(StopTrainingHook(self.max_step))
        hooks = sorted(hooks, key=lambda h: h.priority, reverse=True)
        return hooks, summary_hook

    def clip_grad(self, prefix: str = None):
        # Todo: report clipped and unclipped
        # Todo: allow clip=None but still report grad_norm
        if prefix is None:
            prefix_ = ''
        else:
            prefix_ = f'{prefix}_'
        grad_norm = nested_op(
            lambda model, opti: opti.clip_grad(model.parameters(), prefix)
            if opti is not None else 0.,
            self.model, self.optimizer
        )
        summary = dict(scalars=dict(), histograms=dict())
        if isinstance(grad_norm, dict):
            for key, value in flatten(grad_norm).items():
                summary['scalars'][f'{prefix_}grad_norm_{key}'] = value
                # underscore was necessary to obtain unique keys to prevent
                # tensorboard error
                summary['histograms'][
                    f'{prefix_}grad_norm_{key}_']= torch.Tensor([value])
        if isinstance(grad_norm, (list, tuple)):
            for i, value in enumerate(grad_norm):
                summary['scalars'][f'{prefix_}grad_norm_{i}'] = value
                summary['histograms'][f'{prefix_}grad_norm_{i}_'] = \
                    torch.Tensor([value])
        else:
            summary['scalars'][f'{prefix_}grad_norm'] = grad_norm
            summary['histograms'][f'{prefix_}grad_norm_'] = \
                torch.Tensor([grad_norm])
        return summary

    def save_checkpoint(self):
        checkpoint_path = str(
            self.storage_dir / 'checkpoints' / f'ckpt_{self.iteration}')
        if self.use_cuda:
            self.cpu()
        torch.save(
            dict(
                model=nested_op(lambda m: m.state_dict(), self.model),
                iteration=self.iteration,
                epoch=self.epoch,
                optimizer=nested_op(
                    lambda opti: opti and opti.state_dict(), self.optimizer)
            ),
            checkpoint_path
        )
        if self.use_cuda:
            self.cuda(self.gpu_device)
        print(f"{datetime.now()}: Saved model and optimizer state at iteration "
              f"{self.iteration} to {checkpoint_path}")

    def load_checkpoint(self, checkpoint_path):
        """
        Function should not be modified to accept a folder alone to avoid
        a confusion between best snapshot (for test) and last snapshot
        (resume).

        Args:
            checkpoint_path:

        Returns:

        """
        assert os.path.isfile(checkpoint_path), checkpoint_path
        checkpoint_dict = torch.load(str(checkpoint_path), map_location='cpu')
        nested_op(
            lambda m, d: m.load_state_dict(d),
            self.model, checkpoint_dict['model']
        )
        iteration = checkpoint_dict['iteration']
        self.iteration = iteration + 1
        self.epoch = checkpoint_dict['epoch']
        nested_op(
            lambda opti, d: opti.load_state_dict(d)
            if opti is not None else None,
            self.optimizer, checkpoint_dict['optimizer']
        )
        print(f"Loaded checkpoint '{checkpoint_path}' (iteration {iteration})")

    def cpu(self):
        nested_op(lambda m: m.cpu(), self.model)
        nested_op(
            lambda opti: opti.cpu() if opti is not None else None,
            self.optimizer
        )

    def cuda(self, device):
        nested_op(lambda m: m.cuda(device), self.model)
        nested_op(
            lambda opti: opti.cuda(device) if opti is not None else None,
            self.optimizer
        )