# -*- coding: utf-8 -*-

# (C) Copyright 2020, 2021 IBM. All Rights Reserved.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Test for different utility functionality."""

from tempfile import TemporaryFile
from copy import deepcopy

from numpy import array
from numpy.random import rand
from numpy.testing import assert_array_almost_equal, assert_raises
from torch import Tensor, load, save
from torch.nn import Module, Sequential
from torch.nn.functional import mse_loss

from aihwkit.nn import AnalogConv2d
from aihwkit.optim import AnalogSGD
from aihwkit.simulator.configs import SingleRPUConfig, FloatingPointRPUConfig
from aihwkit.simulator.configs.devices import ConstantStepDevice, LinearStepDevice
from aihwkit.simulator.configs.utils import IOParameters, UpdateParameters
from aihwkit.exceptions import TileError, ModuleError

from .helpers.decorators import parametrize_over_layers
from .helpers.layers import Conv2d, Conv2dCuda, Linear, LinearCuda
from .helpers.testcases import ParametrizedTestCase
from .helpers.tiles import FloatingPoint, ConstantStep, Inference


@parametrize_over_layers(
    layers=[Linear, Conv2d, LinearCuda, Conv2dCuda],
    tiles=[FloatingPoint, ConstantStep, Inference],
    biases=[True, False]
)
class SerializationTest(ParametrizedTestCase):
    """Tests for serialization."""

    @staticmethod
    def train_model(model, loss_func, x_b, y_b):
        """Train the model."""
        opt = AnalogSGD(model.parameters(), lr=0.5)
        opt.regroup_param_groups(model)

        epochs = 1
        for _ in range(epochs):
            pred = model(x_b)
            loss = loss_func(pred, y_b)

            loss.backward()
            opt.step()
            opt.zero_grad()

    @staticmethod
    def get_layer_and_tile_weights(model):
        """Return the weights and biases of the model and the tile."""
        weight = model.weight.data.detach().cpu().numpy()
        if model.use_bias:
            bias = model.bias.data.detach().cpu().numpy()
        else:
            bias = None

        analog_weight, analog_bias = model.analog_tile.get_weights()
        analog_weight = analog_weight.detach().cpu().numpy().reshape(weight.shape)
        if model.use_bias:
            analog_bias = analog_bias.detach().cpu().numpy()
        else:
            analog_bias = None

        return weight, bias, analog_weight, analog_bias

    def test_save_load_state_dict_train(self):
        """Test saving and loading using a state dict after training."""
        model = self.get_layer()

        # Perform an update in order to modify tile weights and biases.
        loss_func = mse_loss
        if isinstance(model, AnalogConv2d):
            input_x = Tensor(rand(2, 2, 3, 3))*0.2
            input_y = Tensor(rand(2, 3, 4, 4))*0.2
        else:
            input_x = Tensor(rand(2, model.in_features))*0.2
            input_y = Tensor(rand(2, model.out_features))*0.2

        if self.use_cuda:
            input_x = input_x.cuda()
            input_y = input_y.cuda()

        self.train_model(model, loss_func, input_x, input_y)

        # Keep track of the current weights and biases for comparing.
        (model_weights, model_biases,
         tile_weights, tile_biases) = self.get_layer_and_tile_weights(model)

        # now the tile weights should be out of sync
        assert_raises(AssertionError, assert_array_almost_equal, model_weights, tile_weights)
        if self.bias:
            assert_raises(AssertionError, assert_array_almost_equal, model_biases, tile_biases)

        # Save the model to a file.
        with TemporaryFile() as file:
            save(model.state_dict(), file)
            # Create a new model and load its state dict.
            file.seek(0)
            new_model = self.get_layer()
            new_model.load_state_dict(load(file))

        # Compare the new model weights and biases. they should now be in sync
        (new_model_weights, new_model_biases,
         new_tile_weights, new_tile_biases) = self.get_layer_and_tile_weights(new_model)

        assert_array_almost_equal(tile_weights, new_model_weights)
        assert_array_almost_equal(tile_weights, new_tile_weights)
        if self.bias:
            assert_array_almost_equal(tile_biases, new_model_biases)
            assert_array_almost_equal(tile_biases, new_tile_biases)

    def test_save_load_model(self):
        """Test saving and loading a model directly."""
        model = self.get_layer()

        # Keep track of the current weights and biases for comparing.
        (model_weights, model_biases,
         tile_weights, tile_biases) = self.get_layer_and_tile_weights(model)
        assert_array_almost_equal(model_weights, tile_weights)
        if self.bias:
            assert_array_almost_equal(model_biases, tile_biases)

        # Save the model to a file.
        with TemporaryFile() as file:
            save(model, file)
            # Load the model.
            file.seek(0)
            new_model = load(file)

        # Compare the new model weights and biases.
        (new_model_weights, new_model_biases,
         new_tile_weights, new_tile_biases) = self.get_layer_and_tile_weights(new_model)

        assert_array_almost_equal(model_weights, new_model_weights)
        assert_array_almost_equal(tile_weights, new_tile_weights)
        if self.bias:
            assert_array_almost_equal(model_biases, new_model_biases)
            assert_array_almost_equal(tile_biases, new_tile_biases)

        # Asserts over the AnalogContext of the new model.
        self.assertTrue(hasattr(new_model.analog_tile.analog_ctx, 'analog_tile'))
        self.assertIsInstance(new_model.analog_tile.analog_ctx.analog_tile,
                              model.analog_tile.__class__)

    def test_save_load_meta_parameter(self):
        """Test saving and loading a device with custom parameters."""
        # Create the device and the array.
        rpu_config = SingleRPUConfig(
            forward=IOParameters(inp_noise=0.321),
            backward=IOParameters(inp_noise=0.456),
            update=UpdateParameters(desired_bl=78),
            device=ConstantStepDevice(w_max=0.987)
        )

        model = self.get_layer(rpu_config=rpu_config)

        # Save the model to a file.
        with TemporaryFile() as file:
            save(model, file)
            # Load the model.
            file.seek(0)
            new_model = load(file)

        # Assert over the new model tile parameters.
        parameters = new_model.analog_tile.tile.get_parameters()
        self.assertAlmostEqual(parameters.forward_io.inp_noise, 0.321)
        self.assertAlmostEqual(parameters.backward_io.inp_noise, 0.456)
        self.assertAlmostEqual(parameters.update.desired_bl, 78)

    def test_save_load_hidden_parameters(self):
        """Test saving and loading a device with hidden parameters."""
        # Create the device and the array.
        model = self.get_layer()
        hidden_parameters = model.analog_tile.tile.get_hidden_parameters()

        # Save the model to a file.
        with TemporaryFile() as file:
            save(model, file)
            # Load the model.
            file.seek(0)
            new_model = load(file)

        # Assert over the new model tile parameters.
        new_hidden_parameters = new_model.analog_tile.tile.get_hidden_parameters()
        assert_array_almost_equal(hidden_parameters, new_hidden_parameters)

    def test_save_load_alpha_scale(self):
        """Test saving and loading a device with alpha_scale."""
        # Create the device and the array.
        model = self.get_layer()
        alpha = 2.0
        model.analog_tile.tile.set_alpha_scale(alpha)

        # Save the model to a file.
        with TemporaryFile() as file:
            save(model, file)
            # Load the model.
            file.seek(0)
            new_model = load(file)

        # Assert over the new model tile parameters.
        alpha_new = new_model.analog_tile.tile.get_alpha_scale()
        assert_array_almost_equal(array(alpha), array(alpha_new))

    def test_save_load_weight_scaling_omega(self):
        """Test saving and loading a device with weight scaling omega."""
        model = self.get_layer(weight_scaling_omega=0.5)

        alpha = model.analog_tile.tile.get_alpha_scale()
        self.assertNotEqual(alpha, 1.0)

        # Save the model to a file.
        with TemporaryFile() as file:
            save(model, file)
            # Load the model.
            file.seek(0)
            new_model = load(file)

        # Assert over the new model tile parameters.
        alpha_new = new_model.analog_tile.tile.get_alpha_scale()
        assert_array_almost_equal(array(alpha), array(alpha_new))

    def test_save_load_state_dict_hidden_parameters(self):
        """Test saving and loading via state_dict with hidden parameters."""
        # Create the device and the array.
        model = self.get_layer()
        hidden_parameters = model.analog_tile.tile.get_hidden_parameters()

        # Save the model to a file.
        with TemporaryFile() as file:
            save(model.state_dict(), file)
            # Load the model.
            file.seek(0)
            new_model = self.get_layer()
            new_model.load_state_dict(load(file))

        # Assert over the new model tile parameters.
        new_hidden_parameters = new_model.analog_tile.tile.get_hidden_parameters()
        assert_array_almost_equal(hidden_parameters, new_hidden_parameters)

    def test_state_dict_children_layers_sequential(self):
        """Test using the state_dict with children analog layers via Sequential."""
        children_layer = self.get_layer()
        model = Sequential(children_layer)

        # Keep track of the current weights and biases for comparing.
        (model_weights, model_biases,
         tile_weights, tile_biases) = self.get_layer_and_tile_weights(children_layer)

        self.assertIn('0.analog_tile_state', model.state_dict())

        # Update the state_dict of a new model.
        new_children_layer = self.get_layer()
        new_model = Sequential(new_children_layer)
        new_model.load_state_dict(model.state_dict())

        # Compare the new model weights and biases.
        (new_model_weights, new_model_biases, new_tile_weights, new_tile_biases) = \
            self.get_layer_and_tile_weights(new_children_layer)

        assert_array_almost_equal(model_weights, new_model_weights)
        assert_array_almost_equal(tile_weights, new_tile_weights)
        if self.bias:
            assert_array_almost_equal(model_biases, new_model_biases)
            assert_array_almost_equal(tile_biases, new_tile_biases)

    def test_state_dict_children_layers_subclassing(self):
        """Test using the state_dict with children analog layers via subclassing."""

        class CustomModule(Module):
            """Module that defines its children layers via custom attributes."""
            # pylint: disable=abstract-method
            def __init__(self, layer: Module):
                super().__init__()
                self.custom_child = layer

        children_layer = self.get_layer()
        model = CustomModule(children_layer)

        # Keep track of the current weights and biases for comparing.
        (model_weights, model_biases, tile_weights, tile_biases) = \
            self.get_layer_and_tile_weights(children_layer)

        self.assertIn('custom_child.analog_tile_state', model.state_dict())

        # Update the state_dict of a new model.
        new_children_layer = self.get_layer()
        new_model = CustomModule(new_children_layer)
        new_model.load_state_dict(model.state_dict())

        # Compare the new model weights and biases.
        (new_model_weights, new_model_biases, new_tile_weights, new_tile_biases) = \
            self.get_layer_and_tile_weights(new_children_layer)

        assert_array_almost_equal(model_weights, new_model_weights)
        assert_array_almost_equal(tile_weights, new_tile_weights)
        if self.bias:
            assert_array_almost_equal(model_biases, new_model_biases)
            assert_array_almost_equal(tile_biases, new_tile_biases)

    def test_state_dict_analog_strict(self):
        """Test the `strict` flag for analog layers."""
        model = self.get_layer()
        state_dict = model.state_dict()

        # Remove the analog key from the state dict.
        del state_dict['analog_tile_state']

        # Check that it fails when using `strict`.
        with self.assertRaises(RuntimeError) as context:
            model.load_state_dict(state_dict, strict=True)
        self.assertIn('Missing key', str(context.exception))

        # Check that it passes when not using `strict`.
        model.load_state_dict(state_dict, strict=False)

    def test_state_dict(self):
        """Test creating a new model using a state dict, without saving to disk."""
        model = self.get_layer()
        state_dict = model.state_dict()

        new_model = self.get_layer()
        new_model.load_state_dict(state_dict)

        # Asserts over the AnalogContext of the new model.
        self.assertTrue(hasattr(new_model.analog_tile.analog_ctx, 'analog_tile'))
        self.assertIsInstance(new_model.analog_tile.analog_ctx.analog_tile,
                              model.analog_tile.__class__)

    def test_hidden_parameter_mismatch(self):
        """Test for error if tile structure mismatches."""
        model = self.get_layer()
        state_dict = model.state_dict()

        # Create the device and the array.
        rpu_config = SingleRPUConfig(
            device=LinearStepDevice()  # different hidden structure
        )

        new_model = self.get_layer(rpu_config=rpu_config)
        if new_model.analog_tile.__class__.__name__ != model.analog_tile.__class__.__name__:
            with self.assertRaises(TileError):
                self.assertRaises(new_model.load_state_dict(state_dict))

    def test_load_state_load_rpu_config(self):
        """Test creating a new model using a state dict, while using a different RPU config."""

        # Create the device and the array.

        model = self.get_layer()
        state_dict = model.state_dict()

        rpu_config = deepcopy(model.analog_tile.rpu_config)

        # Skipped for FP
        if isinstance(rpu_config, FloatingPointRPUConfig):
            return

        old_value = rpu_config.forward.inp_noise
        rpu_config.forward.inp_noise = 0.51

        # Test restore_rpu_config=False
        new_model = self.get_layer(rpu_config=rpu_config)
        new_model.load_state_dict(state_dict, load_rpu_config=False)

        parameters = new_model.analog_tile.tile.get_parameters()
        self.assertAlmostEqual(parameters.forward_io.inp_noise, 0.51)

        # Test restore_rpu_config=True
        new_model = self.get_layer(rpu_config=rpu_config)
        new_model.load_state_dict(state_dict, load_rpu_config=True)

        parameters = new_model.analog_tile.tile.get_parameters()
        self.assertAlmostEqual(parameters.forward_io.inp_noise, old_value)

    def test_load_state_load_rpu_config_wrong(self):
        """Test creating a new model using a state dict, while using a different RPU config."""

        # Create the device and the array.
        model = self.get_layer()
        state_dict = model.state_dict()

        # Skipped for FP
        if isinstance(model.analog_tile.rpu_config, FloatingPointRPUConfig):
            return

        rpu_config = FloatingPointRPUConfig()

        new_model = self.get_layer(rpu_config=rpu_config)
        assert_raises(ModuleError, new_model.load_state_dict, state_dict, load_rpu_config=False)
