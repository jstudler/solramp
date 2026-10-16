# SPDX-License-Identifier: AGPL-3.0-or-later
"""Allow ``python -m solramp``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
