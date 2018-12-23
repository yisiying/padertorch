import unittest
import padertorch as pt
import numpy as np
import torch


class TestDeepClusteringModel(unittest.TestCase):
    # TODO: Test forward deterministic if not train

    def setUp(self):
        self.model = pt.models.bss.DeepClusteringModel()

        self.T = 100
        self.B = 4
        self.E = 20
        self.K = 2
        self.F = 257
        self.num_frames = [100, 90, 80, 70]
        self.inputs = {
            'Y_abs': [
                np.abs(np.random.normal(
                    size=(num_frames_, self.F)
                )).astype(np.float32)
                for num_frames_ in self.num_frames
            ],
            'target_mask': [
                np.abs(np.random.choice(
                    [0, 1],
                    size=(num_frames_, self.K, self.F)
                )).astype(np.float32)
                for num_frames_ in self.num_frames
            ]
        }

    def test_signature(self):
        assert callable(getattr(self.model, 'forward', None))
        assert callable(getattr(self.model, 'review', None))

    def test_forward(self):
        inputs = pt.data.batch_to_device(self.inputs)
        model_out = self.model(inputs)

        for embedding, num_frames in zip(model_out, self.num_frames):
            expected_shape = (num_frames, self.E, self.F)
            assert embedding.shape == expected_shape, embedding.shape

    def test_review(self):
        inputs = pt.data.batch_to_device(self.inputs)
        mask = self.model(inputs)
        review = self.model.review(inputs, mask)

        assert 'losses' in review, review.keys()
        assert 'dc_loss' in review['losses'], review['losses'].keys()

    def test_minibatch_equal_to_single_example(self):
        inputs = pt.data.batch_to_device(self.inputs)
        mask = self.model(inputs)
        review = self.model.review(inputs, mask)
        actual_loss = review['losses']['dc_loss']

        reference_loss = list()
        for observation, target_mask in zip(
            self.inputs['Y_abs'],
            self.inputs['target_mask'],
        ):
            inputs = {
                'Y_abs': [observation],
                'target_mask': [target_mask],
            }
            inputs = pt.data.batch_to_device(inputs)
            mask = self.model(inputs)
            review = self.model.review(inputs, mask)
            reference_loss.append(review['losses']['dc_loss'])

        reference_loss = torch.mean(torch.stack(reference_loss))

        np.testing.assert_allclose(
            actual_loss.detach().numpy(),
            reference_loss.detach().numpy(),
            atol=1e-6
        )


class TestPermutationInvariantTrainingModel(unittest.TestCase):
    # TODO: Test forward deterministic if not train

    def setUp(self):
        self.model = pt.models.bss.PermutationInvariantTrainingModel()

        self.T = 100
        self.B = 4
        self.K = 2
        self.F = 257
        self.num_frames = [100, 90, 80, 70]
        self.inputs = {
            'Y_abs': [
                np.abs(np.random.normal(
                    size=(num_frames_, self.F)
                )).astype(np.float32)
                for num_frames_ in self.num_frames
            ],
            'X_abs': [
                np.abs(np.random.normal(
                    size=(num_frames_, self.K, self.F)
                )).astype(np.float32)
                for num_frames_ in self.num_frames
            ]
        }

    def test_signature(self):
        assert callable(getattr(self.model, 'forward', None))
        assert callable(getattr(self.model, 'review', None))

    def test_forward(self):
        inputs = pt.data.batch_to_device(self.inputs)
        mask = self.model(inputs)

        for m, t in zip(mask, inputs['X_abs']):
            np.testing.assert_equal(m.size(), t.size())

    def test_review(self):
        inputs = pt.data.batch_to_device(self.inputs)
        mask = self.model(inputs)
        review = self.model.review(inputs, mask)

        assert 'losses' in review, review.keys()
        assert 'pit_mse_loss' in review['losses'], review['losses'].keys()

    def test_minibatch_equal_to_single_example(self):
        inputs = pt.data.batch_to_device(self.inputs)
        mask = self.model(inputs)
        review = self.model.review(inputs, mask)
        actual_loss = review['losses']['pit_mse_loss']

        reference_loss = list()
        for observation, target in zip(
            self.inputs['Y_abs'],
            self.inputs['X_abs'],
        ):
            inputs = {
                'Y_abs': [observation],
                'X_abs': [target],
            }
            inputs = pt.data.batch_to_device(inputs)
            mask = self.model(inputs)
            review = self.model.review(inputs, mask)
            reference_loss.append(review['losses']['pit_mse_loss'])

        reference_loss = torch.mean(torch.stack(reference_loss))

        np.testing.assert_allclose(
            actual_loss.detach().numpy(),
            reference_loss.detach().numpy(),
            atol=1e-6
        )
