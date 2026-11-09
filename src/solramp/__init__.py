# SPDX-License-Identifier: AGPL-3.0-or-later
"""solramp - sun-driven exposure ramping for day-to-night timelapses.

The package is split into small, individually testable modules:

* :mod:`solramp.camera_tables` - camera-specific ISO/aperture/shutter tables + stop math
* :mod:`solramp.exposure`      - pure-numpy frame metrics (clipping, EV, saturation)
* :mod:`solramp.raw_analysis`  - rawpy wrapper that turns a CR2 into frame metrics
* :mod:`solramp.astral_phase`  - sun-altitude based day/night phase detection
* :mod:`solramp.ramping`       - the exposure ramping decision engine
* :mod:`solramp.metadata`      - YAML side-car store (with resume support)
* :mod:`solramp.config`        - TOML + CLI configuration merge & validation
* :mod:`solramp.camera`        - transport-agnostic gphoto2 camera session management
* :mod:`solramp.camera_transport` - concrete PTP-IP and USB transport backends
* :mod:`solramp.scheduler`     - capture / download / analyse worker threads
* :mod:`solramp.pipeline`      - top-level orchestration & graceful shutdown
* :mod:`solramp.cli`           - command line entry point
"""

__version__ = "0.1.0"
