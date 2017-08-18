#!/usr/bin/env python
# encoding: utf-8
#
# Licensed under a 3-clause BSD license.
#
# map.py
#
# Created by José Sánchez-Gallego on 26 Jun 2016.


from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

from distutils import version
import os
import warnings
from copy import deepcopy
import operator

from astropy.io import fits
from astropy.units import Quantity

import numpy as np

import marvin
import marvin.api.api
import marvin.core.marvin_pickle
import marvin.core.exceptions
import marvin.tools.maps
import marvin.utils.plot.map
from marvin.utils.general.general import add_doc

from marvin.core.exceptions import MarvinError, MarvinUserWarning
from marvin.utils.dap.datamodel.base import Property

try:
    import sqlalchemy
except ImportError:
    sqlalchemy = None


class Map(Quantity):
    """Describes a single DAP map in a Maps object.

    Unlike a ``Maps`` object, which contains all the information from a DAP
    maps file, this class represents only one of the multiple 2D maps contained
    within. For instance, ``Maps`` may contain emission line maps for multiple
    channels. A ``Map`` would be, for example, the map for ``emline_gflux`` and
    channel ``ha_6564``.

    A ``Map`` returns an astropy 2D Quantity-like array with additional
    attributes for ``ivar`` and ``mask``.

    A ``Map`` is normally initialised from a ``Maps`` by calling the
    :func:`~marvin.tools.maps.Maps.getMap` method. It can be initialialised
    directly by providing a ``Maps`` instance, the ``property_name`` of the
    property to retrieve, and the ``channel``, if necessary. Alternatively,
    a set of ``value``, ``unit``, ``ivar``, and ``mask`` can be passed.

    Parameters:
        maps (:class:`~marvin.tools.maps.Maps` object):
            The :class:`~marvin.tools.maps.Maps` instance from which we
            are extracting the ``Map``.
        property_name (str):
            The category of the map to be extractred (e.g., ``'emline_gflux'``)
            or the full property name including channel
            (``'emline_gflux_ha_6564'``).
        channel (str or None):
            If the ``property`` contains multiple channels, the channel to use,
            e.g., ``ha_6564'. Otherwise, ``None``.

    """

    def __new__(cls, maps=None, property_name=None, channel=None, value=None, unit=None, ivar=None,
                mask=None, dtype=None, copy=True, *args, **kwargs):

        if maps is not None and property_name is not None:

            assert value is None and unit is None, \
                'when initialising a Map from a Maps, value and unit must be None'

            return cls._init_map_from_maps(maps, property_name, channel,
                                           dtype=dtype, copy=copy)

        elif value is not None and unit is not None:
            return cls._init_map_from_value(cls, value, unit, dtype=dtype, copy=copy)

        else:
            raise MarvinError('incorrect combination of input parameters.')

    def __repr__(self):

        if np.isscalar(self.value):
            return super(Map, self).__repr__()
        else:
            return ('<Marvin Map (plateifu={0.maps.plateifu!r}, property={1!r}, '
                    'channel={0.channel!r})>\n{2!r} {3}'.format(self, self.property.name,
                                                                self.value, self.unit.to_string()))

    def __getitem__(self, sl):

        new_obj = super(Map, self).__getitem__(sl)

        if type(new_obj) is not type(self):
            new_obj = self._new_view(new_obj)

        new_obj._set_unit(self.unit)

        new_obj.ivar = self.ivar.__getitem__(sl) if self.ivar is not None else self.ivar
        new_obj.mask = self.mask.__getitem__(sl) if self.mask is not None else self.mask

        return new_obj

    def __deepcopy__(self, memo):
        return Map(maps=deepcopy(self.maps, memo),
                   property_name=deepcopy(self.property.full(), memo),
                   channel=deepcopy(self.channel, memo))

    def __array_finalize__(self, obj):

        if obj is None:
            return

        self.property = getattr(obj, 'property', None)
        self.channel = getattr(obj, 'channel', None)
        self.maps = getattr(obj, 'maps', None)
        self.release = getattr(obj, 'release', None)
        self._datamodel = getattr(obj, '_datamodel', None)

        self.ivar = getattr(obj, 'ivar', None)
        self.mask = getattr(obj, 'mask', None)

    @classmethod
    def _init_map_from_maps(cls, maps, property_name, channel, dtype=None, copy=True):
        """Initialise a Map from a Maps."""

        assert isinstance(maps, marvin.tools.maps.Maps)

        maps = maps
        datamodel = maps._datamodel

        if isinstance(property_name, Property):
            prop = property_name
        else:
            prop = maps._match_properties(property_name, channel=channel)

        assert prop in datamodel.properties, 'failed sanity check. Property does not match.'

        channel = prop.channel

        release = maps.release

        if maps.data_origin == 'file':
            value, ivar, mask, header = cls._get_from_file(maps, prop)
        elif maps.data_origin == 'db':
            value, ivar, mask, header = cls._get_from_db(maps, prop)
        elif maps.data_origin == 'api':
            value, ivar, mask, header = cls._get_from_api(maps, prop)

        unit = prop.unit
        value = value * prop.scale

        obj = cls._init_map_from_value(value, unit, ivar=ivar, mask=mask,
                                       dtype=dtype, copy=copy)

        obj.property = prop
        obj.channel = channel

        obj.header = header
        obj.scale = 1

        obj.maps = maps
        obj.release = release
        obj._datamodel = datamodel

        obj.ivar = (np.array(ivar) / (prop.scale ** 2)) if ivar is not None else None
        obj.mask = np.array(mask) if mask is not None else None

        return obj

    @classmethod
    def _init_map_from_value(cls, value, unit, ivar=None, mask=None, dtype=None, copy=True):
        """Initialise a Map from a value and a unit."""

        obj = Quantity(value, unit=unit, dtype=dtype, copy=copy)
        obj = obj.view(cls)
        obj._set_unit(unit)

        obj.ivar = np.array(ivar) if ivar is not None else None
        obj.mask = np.array(mask) if mask is not None else None

        return obj

    @staticmethod
    def _get_from_file(maps, prop):
        """Initialise the Map from a ``Maps`` with ``data_origin='file'``."""

        header = maps.data[prop.name].header

        if prop.channel is not None:
            channel_idx = prop.channel.idx
            value = maps.data[prop.name].data[channel_idx]
            ivar = maps.data[prop.name + '_ivar'].data[channel_idx] if prop.ivar else None
            mask = maps.data[prop.name + '_mask'].data[channel_idx] if prop.mask else None
        else:
            value = maps.data[prop.name].data
            ivar = maps.data[prop.name + '_ivar'].data if prop.ivar else None
            mask = maps.data[prop.name + '_mask'].data if prop.mask else None

        return value, ivar, mask, header

    @staticmethod
    def _get_from_db(maps, prop):
        """Initialise the Map from a ``Maps`` with ``data_origin='db'``."""

        mdb = marvin.marvindb

        if not mdb.isdbconnected:
            raise marvin.core.exceptions.MarvinError('No db connected')

        if sqlalchemy is None:
            raise marvin.core.exceptions.MarvinError('sqlalchemy required to access the local DB.')

        if version.StrictVersion(maps._dapver) <= version.StrictVersion('1.1.1'):
            table = mdb.dapdb.SpaxelProp
        else:
            table = mdb.dapdb.SpaxelProp5

        fullname_value = prop.db_column()
        value = mdb.session.query(getattr(table, fullname_value)).filter(
            table.file_pk == maps.data.pk).order_by(table.spaxel_index).all()
        value = np.array(value).reshape(maps.shape).T
        ivar = None
        mask = None

        if prop.ivar:
            fullname_ivar = prop.db_column(ext='ivar')
            ivar = mdb.session.query(getattr(table, fullname_ivar)).filter(
                table.file_pk == maps.data.pk).order_by(table.spaxel_index).all()
            ivar = np.array(ivar).reshape(maps.shape).T

        if prop.mask:
            fullname_mask = prop.db_column(ext='mask')
            mask = mdb.session.query(getattr(table, fullname_mask)).filter(
                table.file_pk == maps.data.pk).order_by(table.spaxel_index).all()
            mask = np.array(mask).reshape(maps.shape).T

        # Gets the header
        hdus = maps.data.hdus
        header_dict = None
        for hdu in hdus:
            if prop.name.upper() == hdu.extname.name.upper():
                header_dict = hdu.header_to_dict()
                break

        if not header_dict:
            warnings.warn('cannot find the header for property {0}.'.format(prop.name),
                          MarvinUserWarning)
        else:
            header = fits.Header(header_dict)

        return value, ivar, mask, header

    @staticmethod
    def _get_from_api(maps, prop):
        """Initialise the Map from a ``Maps`` with ``data_origin='api'``."""

        url = marvin.config.urlmap['api']['getmap']['url']

        url_full = url.format(
            **{'name': maps.plateifu,
               'property_name': prop.name,
               'channel': prop.channel.name if prop.channel else None,
               'bintype': maps.bintype.name,
               'template': maps.template.name})

        try:
            response = marvin.api.api.Interaction(url_full,
                                                  params={'release': maps._release})
        except Exception as ee:
            raise marvin.core.exceptions.MarvinError(
                'found a problem when getting the map: {0}'.format(str(ee)))

        data = response.getData()

        if data is None:
            raise marvin.core.exceptions.MarvinError(
                'something went wrong. Error is: {0}'.format(response.results['error']))

        value = np.array(data['value'])
        ivar = np.array(data['ivar']) if data['ivar'] is not None else None
        mask = np.array(data['mask']) if data['mask'] is not None else None
        header = fits.Header(data['header'])

        return value, ivar, mask, header

    def save(self, path, overwrite=False):
        """Pickle the map to a file.

        This method will fail if the map is associated to a Maps loaded
        from the db.

        Parameters:
            path (str):
                The path of the file to which the ``Map`` will be saved.
                Unlike for other Marvin Tools that derive from
                :class:`~marvin.core.core.MarvinToolsClass`, ``path`` is
                mandatory for ``Map`` given that the there is no default
                path for a given map.
            overwrite (bool):
                If True, and the ``path`` already exists, overwrites it.
                Otherwise it will fail.

        Returns:
            path (str):
                The realpath to which the file has been saved.
        """
        # check for file extension
        if not os.path.splitext(path)[1]:
            path = os.path.join(path + '.mpf')

        return marvin.core.marvin_pickle.save(self, path=path, overwrite=overwrite)

    @classmethod
    def restore(cls, path, delete=False):
        """Restore a Map object from a pickled file.

        If ``delete=True``, the pickled file will be removed after it has been
        unplickled. Note that, for map objects instantiated from a Maps object
        with ``data_origin='file'``, the original file must exists and be
        in the same path as when the object was first created.
        """
        return marvin.core.marvin_pickle.restore(path, delete=delete)

    @property
    def masked(self):
        """Return a masked array."""

        assert self.mask is not None, 'mask is None'

        return np.ma.array(self.value, mask=self.mask > 0)

    @property
    def error(self):
        """Computes the standard deviation of the measurement."""

        if self.ivar is None:
            return None

        np.seterr(divide='ignore')

        return np.sqrt(1. / self.ivar) * self.unit

    @property
    def snr(self):
        """Return the signal-to-noise ratio for each spaxel in the map."""

        return np.abs(self.value * np.sqrt(self.ivar))

    @staticmethod
    def _add_ivar(ivar1, ivar2, *args, **kwargs):
        return 1. / ((1. / ivar1 + 1. / ivar2))

    @staticmethod
    def _mul_ivar(ivar1, ivar2, value1, value2, value12):
        with np.errstate(divide='ignore', invalid='ignore'):
            sig1 = 1. / np.sqrt(ivar1)
            sig2 = 1. / np.sqrt(ivar2)
            sig12 = abs(value12) * ((sig1 / abs(value1)) + (sig2 / abs(value2)))
            ivar12 = 1. / sig12**2
        return ivar12

    @staticmethod
    def _pow_ivar(ivar, value, power):
        if ivar is None:
            return np.zeros(value.shape)
        else:
            sig = np.sqrt(1. / ivar)
            sig_out = value**power * power * sig * value
            return 1 / sig_out**2.

    @staticmethod
    def _unit_propagation(unit1, unit2, op):
        ops = {'+': operator.add, '-': operator.sub, '*': operator.mul, '/': operator.truediv}

        if op in ['*', '/']:
            unit12 = ops[op](unit1, unit2)
        else:
            if unit1 == unit2:
                unit12 = unit1
            else:
                warnings.warn('Units do not match for map arithmetic.', UserWarning)
                unit12 = None

        return unit12

    @staticmethod
    def _create_history(map1, map2, op):
        map1_history = getattr(map1, 'history', map1.property)
        map2_history = getattr(map2, 'history', map2.property)
        history = '({0} {1} {2})'.format(map1_history, op, map2_history)
        return history

    @staticmethod
    def _create_parents(map1, map2):
        return [getattr(map_, 'parents', map_) for map_ in [map1, map2]]

    def _arith(self, map2, op):
        """Do map arithmetic and correctly handle map attributes."""

        ops = {'+': operator.add, '-': operator.sub, '*': operator.mul, '/': operator.truediv}

        assert self.shape == map2.shape, 'Cannot do map arithmetic on maps of different shapes.'

        ivar_func = {'+': self._add_ivar, '-': self._add_ivar,
                     '*': self._mul_ivar, '/': self._mul_ivar}

        with np.errstate(divide='ignore', invalid='ignore'):
            map12_value = ops[op](self.value, map2.value)

        map1_ivar = self.ivar if self.ivar is not None else np.zeros(self.shape)
        map2_ivar = map2.ivar if map2.ivar is not None else np.zeros(map2.shape)
        map12_ivar = ivar_func[op](map1_ivar, map2_ivar, self.value, map2.value, map12_value)

        map1_mask = self.mask if self.mask is not None else np.zeros(self.shape, dtype=int)
        map2_mask = map2.mask if map2.mask is not None else np.zeros(map2.shape, dtype=int)
        map12_mask = map1_mask & map2_mask

        map12_unit = self._unit_propagation(self.unit, map2.unit, op)

        # TODO test this!
        if self.release != map2.release:
            warnings.warn('Releases do not match in map arithmetic.')

        # TODO TEST appending previous histories
        history = self._create_history(self, map2, op)
        parents = self._create_parents(self, map2)

        return EnhancedMap(value=map12_value, unit=map12_unit, ivar=map12_ivar, mask=map12_mask,
                           scale=self.scale, release=self.release, history=history,
                           parents=parents, copy=True)

    def __add__(self, map2):
        """Add two maps."""
        return self._arith(map2, '+')

    def __sub__(self, map2):
        """Subtract two maps."""
        return self._arith(map2, '-')

    def __mul__(self, map2):
        """Multiply two maps."""
        return self._arith(map2, '*')

    def __div__(self, map2):
        """Divide two maps."""
        return self._arith(map2, '/')

    def __truediv__(self, map2):
        """Divide two maps."""
        return self.__div__(map2)

    def __pow__(self, power):
        """Raise map to power.

        Parameters:
            power (float):
               Power to raise the map values.

        Returns:
            map (:class:`~marvin.tools.map.EnhancedMap` object)
        """
        value = self.value**power
        ivar = self._pow_ivar(self.ivar, self.value, power)
        unit = self.unit**power

        history = '{0}^{1}'.format(getattr(self, 'history', '({})'.format(self.property)), power)
        parents = getattr(self, 'parents', self)

        return EnhancedMap(value=value, unit=unit, ivar=ivar, mask=self.mask, scale=self.scale,
                           release=self.release, history=history, parents=parents, copy=True)

    def inst_sigma_correction(self):
        """Correct for instrumental broadening."""
        if self.property.name == 'stellar_sigma':

            if self.release == 'MPL-4':
                raise marvin.core.exceptions.MarvinError(
                    'Instrumental broadening correction not implemented for MPL-4.')

            map_corr = self.maps['stellar_sigmacorr']

        elif self.property.name == 'emline_gsigma':
            map_corr = self.maps.getMap(property_name='emline_instsigma', channel=self.channel.name)

        else:
            raise marvin.core.exceptions.MarvinError(
                'Cannot correct {0} for instrumental broadening.'.format(self.property.full()))

        return (self**2 - map_corr**2)**0.5

    @add_doc(marvin.utils.plot.map.plot.__doc__)
    def plot(self, *args, **kwargs):
        return marvin.utils.plot.map.plot(dapmap=self, *args, **kwargs)


class EnhancedMap(Map):
    """Creates a Map that has been modified."""

    # TODO remove "property", "channel", "maps", "_datamodel"

    def __new__(cls, value, unit, *args, **kwargs):
        ignore = ['release', 'scale', 'history', 'parents']
        [kwargs.pop(it) for it in ignore if it in kwargs]
        return cls._init_map_from_value(value, unit, *args, **kwargs)

    def __init__(self, *args, **kwargs):
        self.release = kwargs.get('release', None)
        self.scale = kwargs.get('scale', None)
        self.history = kwargs.get('history', None)
        self.parents = kwargs.get('parents', None)

    def __repr__(self):
        return ('<Marvin EnhancedMap {0.history!r}>'
                '\n{0.value!r} {1!r}').format(self, self.unit.to_string())

    def __deepcopy__(self, memo):
        return EnhancedMap(value=deepcopy(self.value, memo), unit=deepcopy(self.unit, memo),
                           ivar=deepcopy(self.ivar, memo), mask=deepcopy(self.mask, memo),
                           scale=deepcopy(self.scale, memo), release=deepcopy(self.release, memo),
                           history=deepcopy(self.history, memo),
                           parents=deepcopy(self.parents, memo), copy=True)

    def _init_map_from_maps(self):
        raise AttributeError("'EnhancedMap' has no attribute '_init_map_from_maps'.")

    def _get_from_file(self):
        raise AttributeError("'EnhancedMap' has no attribute '_get_from_file'.")

    def _get_from_db(self):
        raise AttributeError("'EnhancedMap' has no attribute '_get_from_db'.")

    def _get_from_api(self):
        raise AttributeError("'EnhancedMap' has no attribute '_get_from_api'.")

    def inst_sigma_correction(self):
        """Override Map.inst_sigma_correction with AttributeError."""
        raise AttributeError("'EnhancedMap' has no attribute '_get_from_api'.")
