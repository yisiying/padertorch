import unittest
import padertorch as pt
import numpy as np
import torch

K = pt.modules.mask_estimator.MaskKeys

class TestMaskEstimatorModel(unittest.TestCase):
    # TODO: Test forward deterministic if not train
    C = 1

    def setUp(self):
        model = pt.models.mask_estimator.MaskEstimatorModel
        self.model = model.from_config(model.get_config({}, {}))
        self.T = 100
        self.B = 4
        self.F = 513
        self.num_frames = [100, 90, 80, 70]
        self.inputs = {
            K.OBSERVATION_ABS: [
                np.abs(np.random.normal(
                    size=(self.C, num_frames_, self.F)
                )).astype(np.float32)
                for num_frames_ in self.num_frames
            ],
            K.SPEECH_MASK_TARGET: [
                np.abs(np.random.choice(
                    [0, 1],
                    size=(self.C, num_frames_, self.F)
                )).astype(np.float32)
                for num_frames_ in self.num_frames
            ],
            K.NOISE_MASK_TARGET: [
                np.abs(np.random.choice(
                    [0, 1],
                    size=(self.C, num_frames_, self.F)
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
        for mask, num_frames in zip(model_out[K.SPEECH_MASK_PRED], self.num_frames):
            expected_shape = (self.C, num_frames, self.F)
            assert mask.shape == expected_shape, mask.shape
        for mask, num_frames in zip(model_out[K.SPEECH_MASK_LOGITS], self.num_frames):
            expected_shape = (self.C, num_frames, self.F)
            assert mask.shape == expected_shape, mask.shape

    def test_review(self):
        inputs = pt.data.batch_to_device(self.inputs)
        mask = self.model(inputs)
        review = self.model.review(inputs, mask)

        assert 'losses' in review, review.keys()
        assert 'loss' in review['losses'], review['losses'].keys()

    def test_minibatch_equal_to_single_example(self):
        inputs = pt.data.batch_to_device(self.inputs)
        model = self.model
        mask = model(inputs)
        review = model.review(inputs, mask)
        actual_loss = review['losses']['loss']

        reference_loss = list()

        for observation, target_mask, noise_mask in zip(
            self.inputs[K.OBSERVATION_ABS],
            self.inputs[K.SPEECH_MASK_TARGET],
            self.inputs[K.NOISE_MASK_TARGET],
        ):
            inputs = {
                K.OBSERVATION_ABS: [observation],
                K.SPEECH_MASK_TARGET: [target_mask],
                K.NOISE_MASK_TARGET: [noise_mask]
            }
            inputs = pt.data.batch_to_device(inputs)
            mask = model(inputs)
            review = model.review(inputs, mask)
            reference_loss.append(review['losses']['loss'])

        reference_loss = torch.sum(torch.stack(reference_loss))

        np.testing.assert_allclose(
            actual_loss.detach().numpy(),
            reference_loss.detach().numpy(),
            atol=1e-3
        )

class TestMaskEstimatorMultiChannelModel(TestMaskEstimatorModel):
    C = 4

