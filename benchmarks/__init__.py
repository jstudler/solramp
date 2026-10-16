# SPDX-License-Identifier: AGPL-3.0-or-later
"""Benchmark package.

Marked as a package (like ``tests``) so pytest puts the repository root on
``sys.path``, which lets the benchmarks import the shared ``tests.synth_raw``
fixture generator.
"""
