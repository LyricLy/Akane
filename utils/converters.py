"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from typing import Any


class MemeDict(dict):
    def __getitem__(self, k: str) -> Any:
        for key in self:
            if k in key:
                return super().__getitem__(key)
        raise KeyError(k)
