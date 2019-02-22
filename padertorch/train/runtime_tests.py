import contextlib
import copy
import inspect
import itertools
import os
import tempfile
from pathlib import Path
from unittest import mock

import numpy as np
import torch
import paderbox as pb
import tqdm

import padertorch as pt
import paderbox as pb


def nested_test_assert_allclose(struct1, struct2):
    def assert_func(array1, array2):
        if array1 is None:
            assert array2 is None, 'Validation step has not been deterministic'
        else:
            array1 = pt.utils.to_numpy(array1)
            array2 = pt.utils.to_numpy(array2)
            np.testing.assert_allclose(
                array1, array2,
                rtol=1e-5,
                atol=1e-5,
                err_msg='Validation step has not been deterministic.\n'
                        'This might be caused by layers changing their\n'
                        'internal state such as BatchNorm'
            )

    pb.utils.nested.nested_op(
        assert_func,
        struct1, struct2,
        handle_dataclass=True,
    )


def test_run(
        trainer: 'pt.Trainer',
        train_iterator,
        validation_iterator,
        device=0 if torch.cuda.is_available() else 'cpu',
        test_with_known_iterator_length=False,
):
    """
    Run a test on the trainer instance (i.e. model test).

    Does not work with layers updating their state such as BatchNorm

    Tests:
     - forward (train and validate)
     - deterministic output in eval
     - simple review dict test

    """
    print('Start test run')
    with contextlib.ExitStack() as exit_stack:
        storage_dir = Path(
            exit_stack.enter_context(tempfile.TemporaryDirectory())
        ).expanduser().resolve()

        exit_stack.enter_context(mock.patch.object(
            trainer,
            'storage_dir',
            new=storage_dir,
        ))
        optimizer_step = pb.utils.nested.nested_op(
            lambda x: (exit_stack.enter_context(mock.patch.object(
                x,
                'step',
            )) if x is not None else None), trainer.optimizer)
        exit_stack.enter_context(mock.patch.object(
            trainer,
            'summary_trigger',
            new=(1, 'epoch'),
        ))
        exit_stack.enter_context(mock.patch.object(
            trainer,
            'checkpoint_trigger',
            new=pt.train.trigger.IntervalTrigger(1, 'epoch'),
        ))
        exit_stack.enter_context(mock.patch.object(
            trainer,
            'max_trigger',
            new=(2, 'epoch'),
        ))
        exit_stack.enter_context(mock.patch.object(
            trainer,
            'iteration',
            new=0,
        ))
        exit_stack.enter_context(mock.patch.object(
            trainer,
            'epoch',
            new=0,
        ))

        class SpyMagicMock(mock.MagicMock):
            def __init__(self, *args, **kw):
                super().__init__(*args, **kw)
                self.spyed_return_values = []

            def __call__(self, *args, **kw):
                ret = super().__call__(*args, **kw)
                self.spyed_return_values += [ret]
                return ret

        trainer_step_mock = exit_stack.enter_context(mock.patch.object(
            trainer,
            'step',
            wraps=trainer.step,
            new_callable=SpyMagicMock,
        ))
        validate_mock = exit_stack.enter_context(mock.patch.object(
            trainer,
            'validate',
            wraps=trainer.validate,
            new_callable=SpyMagicMock,
        ))
        get_default_hooks_mock = exit_stack.enter_context(mock.patch.object(
            trainer,
            'get_default_hooks',
            wraps=trainer.get_default_hooks,
            new_callable=SpyMagicMock,
        ))

        class Iterable:
            def __init__(self, data):
                self.data = data

            def __iter__(self):
                yield from self.data

            def __len__(self):
                raise TypeError(
                    f'object of type {self.__class__.__name__} has no len()'
                )

        sub_train_iterator = list(itertools.islice(train_iterator, 2))
        sub_validation_iterator = list(itertools.islice(validation_iterator, 2))

        if not test_with_known_iterator_length:
            sub_train_iterator = Iterable(sub_train_iterator)
            sub_validation_iterator = Iterable(sub_validation_iterator)

        @contextlib.contextmanager
        def ensure_unchanged_parameter(trainer):
            parameters_before = {
                name: parameter.detach().cpu().numpy().copy()
                for name, parameter in trainer.model.named_parameters()
            }

            yield

            parameters_after = {
                name: parameter.detach().cpu().numpy().copy()
                for name, parameter in trainer.model.named_parameters()
            }

            assert parameters_before.keys() == parameters_after.keys(), (
                parameters_before.keys(), parameters_after.keys()
            )
            for k in parameters_before.keys():
                np.testing.assert_equal(
                    parameters_before[k],
                    parameters_after[k],
                )

        # ================ Train Call ===================
        with ensure_unchanged_parameter(trainer):
            trainer.train(
                sub_train_iterator,
                sub_validation_iterator,
                device=device
            )

        def assert_step(x):
            if x is not None:
                assert x.call_count == 4, x.call_count

        pb.utils.nested.nested_op(assert_step, optimizer_step)

        assert validate_mock.call_count == 2, validate_mock.call_count

        assert trainer_step_mock.call_count == 8, trainer_step_mock.call_count
        assert get_default_hooks_mock.call_count == 1, get_default_hooks_mock.call_count

        def trainer_step_mock_to_inputs_output_review(review_mock):
            sig = inspect.signature(review_mock._mock_wraps)
            for call, (output, review) in zip(review_mock.call_args_list,
                                    review_mock.spyed_return_values):
                args, kwargs = tuple(call)
                inputs, = sig.bind(*args, **kwargs).arguments.values()
                yield dict(inputs=inputs, output=output, review=review)


        # trainer_step_mock_to_inputs_output_review
        tr1, tr2, dt1, dt2, tr3, tr4, dt3, dt4 = \
            trainer_step_mock_to_inputs_output_review(
                trainer_step_mock
            )

        nested_test_assert_allclose(dt1['output'], dt3['output'])
        nested_test_assert_allclose(dt2['output'], dt4['output'])
        nested_test_assert_allclose(dt1['review'], dt3['review'])
        nested_test_assert_allclose(dt2['review'], dt4['review'])

        assert 'loss' in dt1['review'], dt1['review']

        allowed_summary_keys = (
            {'loss', 'losses'} | set(
                pt.trainer.SummaryHook.empty_summary_dict().keys()
            )
        )
        if 0 != len(set(dt1['review'].keys()) - set(allowed_summary_keys)):
            got = set(dt1['review'].keys())
            raise ValueError(
                f'Found keys: {got}\n'
                f'Allowed: {allowed_summary_keys}\n'
                f'Delta: {got - allowed_summary_keys}'
            )
        # end trainer_step_mock_to_inputs_output_review

        hooks, = get_default_hooks_mock.spyed_return_values
        for hook in hooks:
            summary = getattr(hook, 'summary', {})
            assert all([
                len(s) == 0 for s in summary.values()
            ]), (hook, summary)

        files = list(storage_dir.glob('*'))
        assert len(files) == 2, files

        for file in files:
            if 'tfevents' in file.name:
                pass
            elif file.name == 'checkpoints':
                checkpoint_names = {f.name for f in file.glob('*')}
                expect = {
                    'ckpt_latest.pth',
                    'ckpt_best_loss.pth',
                    'ckpt_state.json',
                    'ckpt_2.pth',
                    'ckpt_4.pth',
                }
                if checkpoint_names != expect:
                    os.system(f'ls -lha {file}')
                    raise AssertionError((checkpoint_names, expect))

                ckpt_best = (file / 'ckpt_best_loss.pth').resolve().name
                ckpt_last = (file / 'ckpt_latest.pth').resolve().name

                # This assert only works for exact calculations, that is not the case for cuda
                # assert ckpt_best == 'ckpt_2.pth', ckpt_best

                assert ckpt_last == 'ckpt_4.pth', ckpt_last

                # ckpt_state = pb.io.load_json(file / 'ckpt_state.json')
                # assert ckpt_state == {
                #     'latest_checkpoint_path':
                #         '/tmp/tmp_h0sygfv/checkpoints/ckpt_4.pth',
                #     'metrics': {
                #         'loss': {
                #             'criterion': 'min',
                #             'key': 'loss',
                #             'paths': ['ckpt_2.pth'],
                #             'values': [2.5040305852890015],
                #         }
                #     }
                # }, ckpt_state

    print('Successfully finished test run')


def test_run_from_config(
        trainer_config,
        train_iterator,
        validation_iterator,
        test_with_known_iterator_length=False,
):
    trainer_config = copy.deepcopy(trainer_config)

    with tempfile.TemporaryDirectory() as tmp_dir:
        trainer_config['storage_dir'] = tmp_dir

        tmp_dir = Path(tmp_dir)
        t = pt.Trainer.from_config(trainer_config)

        files_before = tuple(tmp_dir.glob('*'))
        if len(files_before) != 0:
            # no event file
            raise Exception(files_before)

        test_run(
            t,
            train_iterator,
            validation_iterator,
            test_with_known_iterator_length=test_with_known_iterator_length
        )

        files_after = tuple(tmp_dir.glob('*'))
        if files_after != files_before:
            raise Exception(files_after, files_before)