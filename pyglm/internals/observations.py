import numpy as np
from scipy.special import gammaln

from hips.distributions.polya_gamma import polya_gamma

from pyglm.deps.pybasicbayes.abstractions import GibbsSampling
from pypolyagamma import pgdrawv, PyRNG

from hips.inference.log_sum_exp import log_sum_exp_sample

class _PolyaGammaAugmentedCountsBase(GibbsSampling):
    """
    Class to keep track of a set of counts and the corresponding Polya-gamma
    auxiliary variables associated with them.
    """
    def __init__(self, X, counts, nbmodel):
        assert counts.ndim == 1
        self.counts = counts.astype(np.int32)
        self.T = counts.shape[0]

        # assert X.ndim == 2 and X.shape[0] == self.T
        self.X = X

        # Keep this pointer to the model
        self.model = nbmodel

        # Initialize auxiliary variables
        sigma = np.asscalar(self.model.sigma)
        self.psi = self.model.mean_activation(X) + \
                   np.sqrt(sigma) * np.random.randn(self.T)
        #
        # self.omega = polya_gamma(np.ones(self.T),
        #                          self.psi.reshape(self.T),
        #                          200).reshape((self.T,))
        self.omega = np.ones(self.T)
        rng = PyRNG()
        pgdrawv(np.ones(self.T, dtype=np.int32), self.psi, self.omega, rng)

    def log_likelihood(self, x):
        return 0

    def rvs(self, size=[]):
        return None

class AugmentedNegativeBinomialCounts(_PolyaGammaAugmentedCountsBase):

    def resample(self, data=None, stats=None):
        """
        Resample omega given xi and psi, then resample psi given omega, X, w, and sigma
        """

        xi = np.int32(self.model.xi)
        mu = self.model.mean_activation(self.X)
        sigma = self.model.sigma
        sigma = np.asscalar(sigma)

        # Resample the auxiliary variables, omega, in Python
        # self.omega = polya_gamma(self.counts.reshape(self.T)+xi,
        #                          self.psi.reshape(self.T),
        #                          200).reshape((self.T,))

        # Create a PyPolyaGamma object and resample with the C code
        # seed = np.random.randint(2**16)
        # ppg = PyPolyaGamma(seed, self.model.trunc)
        # ppg.draw_vec(self.counts+xi, self.psi, self.omega)

        # Resample with Jesse Windle's ported code
        rng = PyRNG()
        pgdrawv(self.counts+xi, self.psi, self.omega, rng)

        # Resample the rates, psi given omega and the regression parameters
        sig_post = 1.0 / (1.0/sigma + self.omega)
        mu_post = sig_post * ((self.counts-xi)/2.0 + mu / sigma)
        self.psi = mu_post + np.sqrt(sig_post) * np.random.normal(size=(self.T,))

    def geweke_resample_counts(self, trunc=100):
        """
        Resample the counts given omega and psi.
        Given omega, the distribution over y is no longer negative binomial.
        Instead, it takes a pretty ugly form. We have,
        log p(y | xi, psi, omega) = c + log Gamma(y+xi) - log y! - y + psi * (y-xi)/2
        """
        xi = self.model.xi
        ys = np.arange(trunc)[:,None]
        lp = gammaln(ys+xi) - gammaln(ys+1) - ys + (ys-xi) / 2.0 * self.psi[None,:]
        self.counts = log_sum_exp_sample(lp, axis=0)


# We can also do logistic regression as a special case!
class AugmentedBernoulliCounts(_PolyaGammaAugmentedCountsBase):
    def resample(self, data=None, stats=None):
        """
        Resample omega given xi and psi, then resample psi given omega, X, w, and sigma
        """

        # Resample the auxiliary variables, omega, in Python
        # self.omega = polya_gamma(np.ones(self.T),
        #                          self.psi.reshape(self.T),
        #                          200).reshape((self.T,))

        # Resample with the C code
        # Create a PyPolyaGamma object
        # seed = np.random.randint(2**16)
        # ppg = PyPolyaGamma(seed, self.model.trunc)
        # ppg.draw_vec(np.ones(self.T, dtype=np.int32), self.psi, self.omega)

        # Resample with Jesse Windle's code
        rng = PyRNG()
        pgdrawv(np.ones(self.T, dtype=np.int32), self.psi, self.omega, rng)

        # Resample the rates, psi given omega and the regression parameters
        mu_prior = self.model.mean_activation(self.X)
        sigma_prior = self.model.sigma
        sigma_prior = np.asscalar(sigma_prior)

        sig_post = 1.0 / (1.0/sigma_prior + self.omega)
        mu_post = sig_post * (self.counts-0.5 + mu_prior / sigma_prior)
        self.psi = mu_post + np.sqrt(sig_post) * np.random.normal(size=(self.T,))

    def geweke_resample_counts(self):
        """
        Resample the counts given omega and psi.
        Given omega, the distribution over y is no longer negative binomial.
        Instead, it takes a pretty ugly form. We have,
        log p(y | xi, psi, omega) = c + log Gamma(y+xi) - log y! - y + psi * (y-xi)/2
        """
        ys = np.arange(2)[None,:]
        psi = self.psi[:,None]
        # omega = self.omega[:,None]
        # lp = -np.log(2.0) + (ys-0.5) * psi - omega * psi**2 / 2.0
        lp = (ys-0.5) * psi
        for t in xrange(self.T):
            self.counts[t] = log_sum_exp_sample(lp[t,:])


# Finally, support the standard Poisson observations, but to be
# consistent with the NB and Bernoulli models we add a bit of
# Gaussian noise to the activation, psi.
class _LinearNonlinearPoissonCountsBase(GibbsSampling):
    """
    Counts s ~ Poisson(log(1+f(psi))) where f is a rectifying nonlinearity.
    """
    def __init__(self, X, counts, neuron, nsteps=3, step_sz=0.1):
        assert counts.ndim == 1
        self.counts = counts.astype(np.int32)
        self.T = counts.shape[0]

        # assert X.ndim == 2 and X.shape[0] == self.T
        self.X = X

        # Keep this pointer to the model
        self.model = neuron

        # Initialize the activation
        self.psi = self.model.mean_activation(X)

        # Set the number of HMC steps
        self.nsteps = nsteps
        self.step_sz = step_sz

    def f(self, x):
        """
        Return the rate for a given activation x
        """
        raise NotImplementedError()

    def grad_f(self, x):
        """
        Return the nonlinear function of psi
        """
        raise NotImplementedError()

    def rvs(self, size=[]):
        return None


    def log_likelihood(self, x):
        """
        Return the the log likelihood of counts given activation x
        """
        rate = self.f(x)
        return self.counts * np.log(rate) - rate

    def grad_log_likelihood(self, x):
        """
        Return the gradient of the log likelihood of counts given activation x
        """
        rate = self.f(x)
        grad_rate = self.grad_f(x)

        return self.counts / rate * grad_rate - grad_rate

    def log_posterior_psi(self, x):
        mu_prior = self.model.mean_activation(self.X)
        sigma_prior = self.model.sigma
        sigma_prior = np.asscalar(sigma_prior)

        return -0.5/sigma_prior * (x-mu_prior)**2 + self.log_likelihood(x)

    def grad_log_posterior_psi(self, x):
        mu_prior = self.model.mean_activation(self.X)
        sigma_prior = self.model.sigma
        sigma_prior = np.asscalar(sigma_prior)

        return -1.0/sigma_prior * (x-mu_prior) + self.grad_log_likelihood(x)

    def resample(self, data=None, stats=None):
        """
        Resample the activation psi given the counts and the model prior
        using Hamiltonian Monte Carlo
        """
        psi_orig = self.psi
        nsteps = self.nsteps
        step_sz = self.step_sz

        # Start at current state
        psi = np.copy(psi_orig)
        # Momentum is simplest for a normal rv
        p = np.random.randn(*np.shape(psi))
        p_curr = np.copy(p)

        # Set a prefactor of -1 since we're working with log probs
        pre = -1.0

        # Evaluate potential and kinetic energies at start of trajectory
        U_curr = pre * self.log_posterior_psi(psi_orig)
        K_curr = np.sum(p_curr**2)/2.0

        # Make a half step in the momentum variable
        p -= step_sz * pre * self.grad_log_posterior_psi(psi)/2.0

        # Alternate L full steps for position and momentum
        for i in np.arange(self.nsteps):
            psi += step_sz*p

            # Full step for momentum except for last iteration
            if i < nsteps-1:
                p -= step_sz * pre * self.grad_log_posterior_psi(psi)
            else:
                p -= step_sz * pre * self.grad_log_posterior_psi(psi)/2.0

        # Negate the momentum at the end of the trajectory to make proposal symmetric?
        p = -p

        # Evaluate potential and kinetic energies at end of trajectory
        U_prop = pre * self.log_posterior_psi(psi)
        K_prop = p**2/2.0

        # Accept or reject new state with probability proportional to change in energy.
        # Ideally this will be nearly 0, but forward Euler integration introduced errors.
        # Exponentiate a value near zero and get nearly 100% chance of acceptance.
        not_accept = np.log(np.random.rand(*psi.shape)) > U_curr-U_prop + K_curr-K_prop
        psi[not_accept] = psi_orig[not_accept]

        self.psi = psi


class ReLuPoissonCounts(_LinearNonlinearPoissonCountsBase):
    """
    Rectified linear Poisson counts.
    """
    def f(self, x):
        return np.log(1.0+np.exp(x))

    def grad_f(self, x):
        return np.exp(x)/(1.0+np.exp(x))


class ExpPoissonCounts(_LinearNonlinearPoissonCountsBase):
    """
    Rectified linear Poisson counts.
    """
    def f(self, x):
        return np.exp(x)

    def grad_f(self, x):
        return np.exp(x)