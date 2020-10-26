#!env python3
# AUTHOR INFORMATION ##########################################################
# file   : bernstein_bijector.py
# brief  : [Description]
#
# author : Marcel Arpogaus
# date   : 2020-09-11 14:14:24
# COPYRIGHT ###################################################################
# Copyright 2020 Marcel Arpogaus
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# NOTES ######################################################################
#
# This project is following the
# [PEP8 style guide](https://www.python.org/dev/peps/pep-0008/)
#
# CHANGELOG ##################################################################
# modified by   : Marcel Arpogaus
# modified time : 2020-10-14 20:24:44
#  changes made : ...
# modified by   : Marcel Arpogaus
# modified time : 2020-09-11 14:14:24
#  changes made : newly written
###############################################################################

# REQUIRED PYTHON MODULES #####################################################
import scipy.interpolate as I

import numpy as np
import tensorflow as tf

from tensorflow_probability import distributions as tfd
from tensorflow_probability import bijectors as tfb

from tensorflow_probability.python.internal import dtype_util
from tensorflow_probability.python.internal import tensor_util
from tensorflow_probability.python.internal import tensorshape_util


class BernsteinBijector(tfb.Bijector):
    """
    Implementing Bernstein polynomials using the `tfb.Bijector` interface for
    transformations of a `Distribution` sample.
    """

    def __init__(self,
                 order: int,
                 theta: tf.Tensor,
                 validate_args: bool = False,
                 name: str = 'bernstein_bijector'):
        """
        Constructs a new instance of a Bernstein polynomial bijector.

        :param      order:          The order of the Bernstein polynomial.
        :type       order:          int
        :param      theta:          The Bernstein coefficients.
        :type       theta:          Tensor
        :param      validate_args:  Whether to validate input with asserts.
                                    Passed to `super()`.
        :type       validate_args:  bool
        :param      name:           The name to give Ops created by the
                                    initializer. Passed to `super()`.
        :type       name:           str
        """
        with tf.name_scope(name) as name:
            dtype = dtype_util.common_dtype([theta], dtype_hint=tf.float32)

            self.theta = tensor_util.convert_nonref_to_tensor(
                theta, dtype=dtype)

            self.order = order

            if tensorshape_util.rank(self.theta.shape) == 1:
                self.batch_shape = tf.TensorShape([1])
            else:
                self.batch_shape = tf.TensorShape([self.theta.shape[0]])

            # Bernstein polynomials of order M,
            # generated by the M + 1 beta-densities
            self.beta_dist_h = tfd.Beta(
                tf.range(1, self.order + 1, dtype=tf.float32),
                tf.range(self.order, 0, -1, dtype=tf.float32))

            # Deviation of the Bernstein polynomials
            self.beta_dist_h_dash = tfd.Beta(
                tf.range(1, self.order, dtype=tf.float32),
                tf.range(self.order - 1, 0, -1, dtype=tf.float32))

            # Cubic splines are used to approximate the inverse
            self.interp = None

            super().__init__(
                forward_min_event_ndims=0,
                validate_args=validate_args,
                dtype=dtype,
                name=name)

    def gen_inverse_interpolation(self) -> None:
        """
        Generates the Spline Interpolation.
        """
        y_fit = np.linspace(.0, 1, self.order * 10,
                            dtype=np.float32)[..., tf.newaxis]

        z_fit = self.forward(y_fit)

        self.z_min = np.min(z_fit, axis=0).reshape(-1, 1)
        self.z_max = np.max(z_fit, axis=0).reshape(-1, 1)

        ips = [I.interp1d(
            x=np.squeeze(z_fit[..., i]),
            y=np.squeeze(y_fit),
            kind='cubic'
        ) for i in range(self.batch_shape[0])]

        def ifn(z):
            y = []
            z_clip = np.clip(z, self.z_min + 1E-5, self.z_max - 1E-5)
            for i, ip in enumerate(ips):
                y.append(ip(z_clip[:, i]).astype(np.float32))
            y = np.stack(y, axis=1)

            return y

        self.interp = ifn

    def _inverse(self, z: tf.Tensor) -> tf.Tensor:
        """
        Returns the inverse Bijector evaluation.

        :param      z:    The input to the inverse evaluation.
        :type       z:    Tensor

        :returns:   The inverse Bijector evaluation.
        :rtype:     Tensor
        """
        if tf.executing_eagerly():
            if (tf.rank(z) == 0):
                def reshape_out(y): return tf.squeeze(y)
            elif z.shape == self.batch_shape:
                # [sample_shape, batch_shape, event_shape]
                z = z[tf.newaxis, ...]
                def reshape_out(y): return y[0]
            elif (tf.rank(z) == 2) and (z.shape[1] == self.batch_shape[0]):
                # [sample_shape, batch_shape, event_shape]
                z = z[..., tf.newaxis]
                def reshape_out(y): return y.mean(axis=1)  # [None,...]
            else:
                z = z[..., tf.newaxis]
                def reshape_out(y): return y[..., 0]

            if self.interp is None:
                self.gen_inverse_interpolation()
            y = reshape_out(self.interp(z))
        else:
            y = z

        return y

    def _forward(self, y: tf.Tensor) -> tf.Tensor:
        """
        Returns the forward Bijector evaluation.

        :param      y:    The input to the forward evaluation.
        :type       y:    Tensor

        :returns:   The forward Bijector evaluation.
        :rtype:     Tensor
        """
        # if (tensorshape_util.rank(y.shape) == 1) and \
        #     (y.shape[0] != self.batch_shape):
        #    #y = tf.transpose(y, [1, 0])
        #    # [sample_shape, batch_shape, event_shape]
        #    y = y[..., tf.newaxis, tf.newaxis]
        #    def reshape_out(z): return z#tf.transpose(z, [1, 0])
        # else:
        y = y[..., tf.newaxis]

        y = tf.clip_by_value(y, 1E-5, 1.0 - 1E-5)
        by = self.beta_dist_h.prob(y)
        z = tf.reduce_mean(by * self.theta, axis=-1)

        return z

    def _forward_log_det_jacobian(self, y):
        # if (tensorshape_util.rank(y.shape) == 1) and \
        #     (y.shape[0] != self.batch_shape):
        #    #y = tf.transpose(y, [1, 0])
        #    # [sample_shape, batch_shape, event_shape]
        #    y = y[..., tf.newaxis, tf.newaxis]
        #    def reshape_out(z): return z#tf.transpose(z, [1, 0])
        # else:
        y = y[..., tf.newaxis]

        y = tf.clip_by_value(y, 1E-5, 1.0 - 1E-5)
        by = self.beta_dist_h_dash.prob(y)
        dtheta = self.theta[..., 1:] - self.theta[..., 0:-1]
        ldj = tf.math.log(tf.reduce_sum(by * dtheta, axis=-1))

        return ldj

    @classmethod
    def constrain_theta(cls: type,
                        theta_unconstrained: tf.Tensor,
                        fn=tf.math.softplus) -> tf.Tensor:
        """
        Class method to calculate theta_1 = h_1, theta_k = theta_k-1 + exp(h_k)

        :param      cls:                  The class as implicit first argument.
        :type       cls:                  type
        :param      theta_unconstrained:  The unconstrained Bernstein
                                          coefficients.
        :type       theta_unconstrained:  Tensor
        :param      fn:                   The used activation function.
        :type       fn:                   Function

        :returns:   The constrained Bernstein coefficients.
        :rtype:     Tensor
        """
        d = tf.concat((tf.zeros_like(theta_unconstrained[..., :1]),
                       theta_unconstrained[..., :1],
                       fn(theta_unconstrained[..., 1:])), axis=-1)
        return tf.cumsum(d[..., 1:], axis=-1)