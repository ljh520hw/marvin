
# TODO: add description to bintype and template
# TODO: add string and latex representation to properties.


from .base import *

from .MPL4 import MPL4
from .MPL5 import MPL5
from .MPL6 import MPL6

# Defines the list of datamodels.
datamodel = DAPDataModelList([MPL4, MPL5, MPL6])


from .plotting import *