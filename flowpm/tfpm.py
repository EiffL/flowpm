""" Core FastPM elements"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import tensorflow as tf
from astropy.cosmology import Planck15
import fastpm
PerturbationGrowth = lambda cosmo, *args, **kwargs: fastpm.background.MatterDominated(Omega0_lambda = cosmo.Ode0,
                                                                                      Omega0_m = cosmo.Om0,
                                                                                      Omega0_k = cosmo.Ok0,
                                                                                      *args, **kwargs)

from .utils import white_noise, c2r3d, r2c3d, cic_paint, cic_readout
from .kernels import fftk, laplace_kernel, gradient_kernel

def linear_field(nc, boxsize, pk, batch_size=1,
                 kvec=None, seed=None, name=None, dtype=tf.float32):
  """Generates a linear field with a given linear power spectrum

  Parameters:
  -----------
  nc: int
    Number of cells in the field

  boxsize: float
    Physical size of the cube, in Mpc/h TODO: confirm units

  pk: interpolator
    Power spectrum to use for the field

  kvec: array
    k_vector corresponding to the cube, optional

  Returns
  ------
  linfield: tensor (batch_size, nc, nc, nc)
    Realization of the linear field with requested power spectrum
  """
  with tf.name_scope(name, "LinearField"):
    if kvec is None:
      kvec = fftk((nc, nc, nc), boxsize, symmetric=False)
    kmesh = sum(kk**2 for kk in kvec)**0.5
    pkmesh = pk(kmesh)

    whitec = white_noise(nc, batch_size=batch_size, seed=seed, type='complex')
    lineark = tf.multiply(whitec, (pkmesh/boxsize**3)**0.5)
    linear = c2r3d(lineark, norm=nc**3, name=name, dtype=dtype)
    return linear

def lpt1(dlin_k, pos, boxsize, kvec=None, name=None):
  """ Run first order LPT on linear density field, returns displacements of particles
      reading out at q. The result has the same dtype as q.

  Parameters:
  -----------
  dlin_k: TODO: @modichirag add documentation

  Returns:
  --------
  displacement: tensor (batch_size, npart, 3)
    Displacement field
  """
  with tf.name_scope(name, "LPT1", [dlin_k, pos]):
    shape = dlin_k.get_shape()
    batch_size, nc = shape[0], shape[1].value
    if kvec is None:
      kvec = fftk((nc, nc, nc), boxsize, symmetric=False)

    lap = tf.cast(laplace_kernel(kvec), tf.complex64)

    displacement = []
    for d in range(3):
      kweight = gradient_kernel(kvec, d, boxsize) * lap
      dispc = tf.multiply(dlin_k, kweight)
      disp = c2r3d(dispc, norm=nc**3)
      displacement.append(cic_readout(disp, pos))
    return tf.stack(displacement, axis=2)

def lpt2_source(dlin_k, boxsize, kvec=None, name=None):
  """ Generate the second order LPT source term.

  Parameters:
  -----------
  dlin_k: TODO: @modichirag add documentation

  Returns:
  --------
  source: tensor (batch_size, nc, nc, nc)
    Source term
  """
  with tf.name_scope(name, "LPT2Source", [dlin_k]):
    shape = dlin_k.get_shape()
    batch_size, nc = shape[0], shape[1].value
    if kvec is None:
      kvec = fftk((nc, nc, nc), boxsize, symmetric=False)
    source = tf.zeros(tf.shape(dlin_k))
    D1 = [1, 2, 0]
    D2 = [2, 0, 1]

    phi_ii = []
    # diagnoal terms
    lap = laplace_kernel(kvec)

    for d in range(3):
        grad = gradient_kernel(kvec, d, boxsize)
        kweight = grad * grad * lap
        phic = tf.multiply(dlin_k, kweight)
        phi_ii.append(c2r3d(phic, norm=nc**3))

    for d in range(3):
        source = tf.add(source, tf.multiply(phi_ii[D1[d]], phi_ii[D2[d]]))

    # free memory
    phi_ii = []
    # off-diag terms
    for d in range(3):
        gradi = gradient_kernel(kvec, D1[d], boxsize)
        gradj = gradient_kernel(kvec, D2[d], boxsize)
        kweight = gradi * gradj * lap
        phic = tf.multiply(dlin_k, kweight)
        phi = c2r3d(phic, norm=nc**3)
        source = tf.subtract(source, tf.multiply(phi, phi))

    source = tf.multiply(source, 3.0/7.)
    return r2c3d(source, norm=nc**3)

def lpt_init(linear, boxsize, a0, order=2, cosmology=Planck15, name=None):
  """ Estimate the initial LPT displacement given an input linear (real) field

  Parameters:
  -----------
  TODO: documentation
  """
  with tf.name_scope(name, "LPTInit", [linear]):
    assert order in (1, 2)
    shape = linear.get_shape()
    batch_size, nc = shape[0], shape[1].value

    dtype = np.float32
    Q = boxsize/nc*np.indices((nc, nc, nc)).reshape(3, -1).T.astype(dtype)
    Q = np.repeat(Q.reshape((1, -1, 3)), batch_size, axis=0)
    pos = Q

    a = a0

    lineark = r2c3d(linear, norm=nc**3)

    pt = PerturbationGrowth(cosmology, a=[a], a_normalize=1.0)
    DX = tf.multiply(dtype(pt.D1(a)) , lpt1(lineark, pos, boxsize))
    P = tf.multiply(dtype(a ** 2 * pt.f1(a) * pt.E(a)) , DX)
    F = tf.multiply(dtype(a ** 2 * pt.E(a) * pt.gf(a) / pt.D1(a)) , DX)
    if order == 2:
      DX2 = tf.multiply(dtype(pt.D2(a)) , lpt1(lpt2_source(lineark, boxsize), pos, boxsize))
      P2 = tf.multiply(dtype(a ** 2 * pt.f2(a) * pt.E(a)) , DX2)
      F2 = tf.multiply(dtype(a ** 2 * pt.E(a) * pt.gf2(a) / pt.D2(a)) , DX2)
      DX = tf.add(DX, DX2)
      P = tf.add(P, P2)
      F = tf.add(F, F2)

    X = tf.add(DX, Q)
    return tf.stack((X, P, F), axis=0)

def kick(state, ai, ac, af, cosmology=Planck15, dtype=np.float32, name=None):
  """Kick the particles given the state
  """
  with tf.name_scope(name, "Kick", [state]):
    pt = PerturbationGrowth(cosmology, a=[ai, ac, af], a_normalize=1.0)
    fac = 1 / (ac ** 2 * pt.E(ac)) * (pt.Gf(af) - pt.Gf(ai)) / pt.gf(ac)
    indices = tf.constant([[1]])
    update = tf.expand_dims(tf.multiply(dtype(fac), state[2]), axis=0)
    shape = state.shape
    update = tf.scatter_nd(indices, update, shape)
    state = tf.add(state, update)
    return state

def drift(state, ai, ac, af, cosmology=Planck15, dtype=np.float32, name=None):
  """Drift the particles given the state"""
  with tf.name_scope(name, "Drift", [state]):
    pt = PerturbationGrowth(cosmology, a=[ai, ac, af], a_normalize=1.0)
    fac = 1 / (ac ** 3 * pt.E(ac)) * (pt.Gp(af) - pt.Gp(ai)) / pt.gp(ac)
    indices = tf.constant([[0]])
    update = tf.expand_dims(tf.multiply(dtype(fac), state[1]), axis=0)
    shape = state.shape
    update = tf.scatter_nd(indices, update, shape)
    state = tf.add(state, update)
    return state

def force(state, boxsize, cosmology=Planck15, pm_nc_factor=1, dtype=tf.float32):
  """
  Estimate force on the particles given a state.

  Parameters:
  -----------
  state: tensor
    Input state tensor of shape (batch_size, nc, nc, nc)

  box_size: float
    Size of the simulation volume (Mpc/h) TODO: check units

  cosmology: astropy.cosmology
    Cosmology object

  pm_nc_factor: int
    TODO: @modichirag please add doc
  """
  with tf.name_scope(name, "Force", [state]):
        
    rho = tf.zeros((batch_size, ncf, ncf, ncf))
    wts = tf.ones(nc**3)
    nbar = nc**3/ncf**3

    rho = cic_paint(rho, tf.multiply(state[0], ncf/bs), wts)
    rho = tf.multiply(rho, 1/nbar)  ###I am not sure why this is not needed here
    delta_k = r2c3d(rho, norm=ncf**3)
    fac = dtype(1.5 * config['cosmology'].Om0)
    update = longrange(config['f_config'], tf.multiply(state[0], ncf/bs), delta_k, split=0, factor=fac)

    update = tf.expand_dims(update, axis=0)

    indices = tf.constant([[2]])
    shape = state.shape
    update = tf.scatter_nd(indices, update, shape)
    mask = tf.stack((tf.ones_like(state[0]), tf.ones_like(state[0]), tf.zeros_like(state[0])), axis=0)
    state = tf.multiply(state, mask)
    state = tf.add(state, update)
    return state


def leapfrog(stages):
    """ Generate a leap frog stepping scheme.
        Parameters
        ----------
        stages : array_like
            Time (a) where force computing stage is requested.
    """
    if len(stages) == 0:
        return

    ai = stages[0]
    # first force calculation for jump starting
    yield 'F', ai, ai, ai
    x, p, f = ai, ai, ai

    for i in range(len(stages) - 1):
        a0 = stages[i]
        a1 = stages[i + 1]
        ah = (a0 * a1) ** 0.5
        yield 'K', p, f, ah
        p = ah
        yield 'D', x, p, a1
        x = a1
        yield 'F', f, x, a1
        f = a1
        yield 'K', p, f, a1
        p = a1


def nbody(state, config, verbose=False, name=None, B=1):
    '''Do the nbody evolution'''
    stepping = leapfrog(config['stages'])
    #if B==1: actions = {'F':Force, 'K':Kick, 'D':Drift}
    #elif B==2: actions = {'F':Force2, 'K':Kick, 'D':Drift}
    actions = {'F':Force, 'K':Kick, 'D':Drift}

    for action, ai, ac, af in stepping:
        if verbose: print(action, ai, ac, af)
        state = actions[action](state, ai, ac, af, config)
    if name is not None:
        state = tf.identity(state, name=name)
    return state
