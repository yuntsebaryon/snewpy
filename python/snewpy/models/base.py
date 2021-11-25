from abc import abstractmethod, ABC

from astropy.io import ascii, fits
from astropy.table import Table, join
from astropy.units.quantity import Quantity

import numpy as np
from scipy.interpolate import interp1d
from scipy.special import loggamma, gamma, lpmv

import os

from warnings import warn
from snewpy.neutrino import Flavor
from snewpy.flavor_transformation import *
from snewpy.flux import Flux

class SupernovaModel(ABC):
    """Base class defining an interface to a supernova model."""
    def __init__(self):
        self.metadata = {}

    def __repr__(self):
        """Default representation of the model.
        """
        # self.__class__ will be something like 
        mod = f"{self.__class__.__name__} Model"
        try:
            mod +=f': {self.filename}'
        except:
            pass
        s = [mod]
        for name, v in self.metadata.items():
            s +=[f"{name:16} : {v}"]
        return '\n'.join(s)
        
    def _repr_markdown_(self):
        """Markdown representation of the model, for Jupyter notebooks.
        """
        mod = f'**{self.__class__.__name__} Model**'
        try:
            mod +=f': {self.filename}'
        except:
            pass
        s = [mod,'']
        if self.metadata:
            s += ['|Parameter|Value|',
                  '|:--------|:----:|']
            for name, v in self.metadata.items():
                try: 
                    s += [f"|{name} | ${v.value:g}$ {v.unit:latex}|"]
                except:
                    s += [f"|{name} | {v} |"]
        return '\n'.join(s)

    @abstractmethod
    def get_time(self):
        """Returns
        -------
            returns array of snapshot times from the simulation
        """
        pass

    @abstractmethod
    def get_initial_spectra(self, t, E, flavors=Flavor):
        """Get neutrino spectra at the source.

        Parameters
        ----------
        t : astropy.Quantity or ndarray of astropy.Quantity
            Times to evaluate initial spectra.
        E : astropy.Quantity or ndarray of astropy.Quantity
            Energies to evaluate the initial spectra.
        flavors: iterable of snewpy.neutrino.Flavor
            Return spectra for these flavors only (default: all)

        Returns
        -------
        initialspectra : dict
            Dictionary of neutrino spectra, keyed by neutrino flavor.
        """
        pass

    def get_initialspectra(self, *args):
        """DO NOT USE! Only for backward compatibility!

        :meta private:
        """
        warn("Please use `get_initial_spectra()` instead of `get_initialspectra()`!", FutureWarning)
        return self.get_initial_spectra(*args)

    def get_transformed_spectra(self, t, E, flavor_xform):
        """Get neutrino spectra after applying oscillation.

        Parameters
        ----------
        t : astropy.Quantity
            Time to evaluate initial and oscillated spectra.
        E : astropy.Quantity or ndarray of astropy.Quantity
            Energies to evaluate the initial and oscillated spectra.
        flavor_xform : FlavorTransformation
            An instance from the flavor_transformation module.

        Returns
        -------
        dict
            Dictionary of transformed spectra, keyed by neutrino flavor.
        """
        initialspectra = self.get_initial_spectra(t, E)
        transformed_spectra = {}

        transformed_spectra[Flavor.NU_E] = \
            flavor_xform.prob_ee(t, E) * initialspectra[Flavor.NU_E] + \
            flavor_xform.prob_ex(t, E) * initialspectra[Flavor.NU_X]

        transformed_spectra[Flavor.NU_X] = \
            flavor_xform.prob_xe(t, E) * initialspectra[Flavor.NU_E] + \
            flavor_xform.prob_xx(t, E) * initialspectra[Flavor.NU_X] 

        transformed_spectra[Flavor.NU_E_BAR] = \
            flavor_xform.prob_eebar(t, E) * initialspectra[Flavor.NU_E_BAR] + \
            flavor_xform.prob_exbar(t, E) * initialspectra[Flavor.NU_X_BAR]

        transformed_spectra[Flavor.NU_X_BAR] = \
            flavor_xform.prob_xebar(t, E) * initialspectra[Flavor.NU_E_BAR] + \
            flavor_xform.prob_xxbar(t, E) * initialspectra[Flavor.NU_X_BAR] 

        return transformed_spectra   


    def get_oscillatedspectra(self, *args):
        """DO NOT USE! Only for backward compatibility!

        :meta private:
        """
        warn("Please use `get_transformed_spectra()` instead of `get_oscillatedspectra()`!", FutureWarning)
        return self.get_transformed_spectra(*args)

    def get_transformed_flux(self, t, E, flavor_xform, distance=10*u.kpc):
        """Get neutrino fluence at the given distance

        Parameters
        ----------
        t : astropy.Quantity
            Time to evaluate initial and oscillated spectra.
        E : astropy.Quantity or ndarray of astropy.Quantity
            Energies to evaluate the initial and oscillated spectra.
        flavor_xform : FlavorTransformation
            An instance from the flavor_transformation module.
        distance: astropy.Quantity
            Distance to supernova

        Returns
        -------
        dict
            Dictionary with the neutrino flux (neutrinos/cm^2/MeV/s), keyed by flavor
        """
        spec = self.get_transformed_spectra(t,E,flavor_xform)
        factor = 1/(4*np.pi*(distance.to('cm'))**2) 
        flavors = list(sorted(spec))
        spec_array = np.stack([spec[f] for f in flavors], axis=0) * factor
        return Flux(data=spec_array,Flavor=flavors, Enu=E, time=t)

def get_value(x):
    """If quantity x has is an astropy Quantity with units, return just the
    value.

    Parameters
    ----------
    x : Quantity, float, or ndarray
        Input quantity.

    Returns
    -------
    value : float or ndarray
    
    :meta private:
    """
    if type(x) == Quantity:
        return x.value
    return x

class PinchedModel(SupernovaModel):
    """Subclass that contains spectra/luminosity pinches"""

    def get_time(self):
        """Get grid of model times.

        Returns
        -------
        time : ndarray
            Grid of times used in the model.
        """
        return self.time

    def get_initial_spectra(self, t, E, flavors=Flavor):
        """Get neutrino spectra/luminosity curves before oscillation.

        Parameters
        ----------
        t : astropy.Quantity
            Time to evaluate initial spectra.
        E : astropy.Quantity or ndarray of astropy.Quantity
            Energies to evaluate the initial spectra.
        flavors: iterable of snewpy.neutrino.Flavor
            Return spectra for these flavors only (default: all)

        Returns
        -------
        initialspectra : dict
            Dictionary of model spectra, keyed by neutrino flavor.
        """
        initialspectra = {}

        # Avoid division by zero in energy PDF below.
        E[E==0] = np.finfo(float).eps * E.unit

        # Estimate L(t), <E_nu(t)> and alpha(t). Express all energies in erg.
        E = E.to_value('erg')

        # Make sure input time uses the same units as the model time grid, or
        # the interpolation will not work correctly.
        t = t.to(self.time.unit)

        E  = np.expand_dims(E, axis=1)
        for flavor in flavors:
            # Use np.interp rather than scipy.interpolate.interp1d because it
            # can handle dimensional units (astropy.Quantity).
            L  = get_value(np.interp(t, self.time, self.luminosity[flavor].to('erg/s'),left=0,right=0))
            Ea = get_value(np.interp(t, self.time, self.meanE[flavor].to('erg')))
            a  = np.interp(t, self.time, self.pinch[flavor])

            # Sanity check to avoid invalid values of Ea, alpha, and L.
            initialspectra[flavor] = np.zeros_like(E, dtype=float) / (u.erg*u.s)
            L  = np.expand_dims(L, axis=0)
            Ea = np.expand_dims(Ea,axis=0)
            a  = np.expand_dims(a, axis=0)
            # For numerical stability, evaluate log PDF and then exponentiate.
            result = \
              np.exp(np.log(L) - (2+a)*np.log(Ea) + (1+a)*np.log(1+a)
                    - loggamma(1+a) + a*np.log(E) - (1+a)*(E/Ea)) / (u.erg * u.s)
            #remove bad values
            result[np.isnan(result)] = 0
            initialspectra[flavor] = result

        return initialspectra


class _GarchingArchiveModel(PinchedModel):
    """Subclass that reads models in the format used in the [Garching Supernova Archive](https://wwwmpa.mpa-garching.mpg.de/ccsnarchive/)."""
    def __init__(self, filename, eos='LS220'):
        """Initialize model.

        Parameters
        ----------
        filename : str
            Absolute or relative path to file prefix, we add nue/nuebar/nux.
        eos : string
            Equation of state used in simulation.
        """
        self.time = {}
        self.luminosity = {}
        self.meanE = {}
        self.pinch = {}

        # Store model metadata.
        self.filename = os.path.basename(filename)
        self.EOS = eos
        self.progenitor_mass = float( (self.filename.split('s'))[1].split('c')[0] )  * u.Msun
        self.metadata = {
            'Progenitor mass':self.progenitor_mass,
            'EOS':self.EOS,
            }
        # Read through the several ASCII files for the chosen simulation and
        # merge the data into one giant table.
        mergtab = None
        for flavor in Flavor:
            _flav = Flavor.NU_X if flavor == Flavor.NU_X_BAR else flavor
            _sfx = _flav.name.replace('_', '').lower()
            _filename = '{}_{}_{}'.format(filename, eos, _sfx)
            _lname  = 'L_{}'.format(flavor.name)
            _ename  = 'E_{}'.format(flavor.name)
            _e2name = 'E2_{}'.format(flavor.name)
            _aname  = 'ALPHA_{}'.format(flavor.name)

            simtab = Table.read(_filename,
                                names=['TIME', _lname, _ename, _e2name],
                                format='ascii')
            simtab['TIME'].unit = 's'
            simtab[_lname].unit = '1e51 erg/s'
            simtab[_aname] = (2*simtab[_ename]**2 - simtab[_e2name]) / (simtab[_e2name] - simtab[_ename]**2)
            simtab[_ename].unit = 'MeV'
            del simtab[_e2name]

            if mergtab is None:
                mergtab = simtab
            else:
                mergtab = join(mergtab, simtab, keys='TIME', join_type='left')
                mergtab[_lname].fill_value = 0.
                mergtab[_ename].fill_value = 0.
                mergtab[_aname].fill_value = 0.
        simtab = mergtab.filled()

        self.time = simtab['TIME'].to('s')

        for flavor in Flavor:
            # Set the dictionary of luminosity, mean energy, and shape
            # parameter keyed by NU_E, NU_X, NU_E_BAR, NU_X_BAR.
            _lname  = 'L_{}'.format(flavor.name)
            self.luminosity[flavor] = simtab[_lname].to('erg/s')

            _ename  = 'E_{}'.format(flavor.name)
            self.meanE[flavor] = simtab[_ename].to('MeV')

            _aname  = 'ALPHA_{}'.format(flavor.name)
            self.pinch[flavor] = simtab[_aname]

