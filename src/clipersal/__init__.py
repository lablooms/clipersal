"""Clipersal (by Lablooms): continuous rolling screen-capture buffer with
save-on-demand. "Catch the moment you bloomed."
"""

# PEP 440 note: "0.1.0-lab.1" is NOT a valid Python package version (pre-release
# labels are only a/b/rc/dev), and hatchling refuses to build it -- the CI
# release pipeline proved that. The PEP-440-sanctioned place for a custom
# downstream label is the local-version segment: "0.1.0+lab.1". Humans read the
# dash spelling instead (release tags, installer, UI), so display surfaces use
# DISPLAY_VERSION.
__version__ = "0.1.0+lab.3"
DISPLAY_VERSION = __version__.replace("+", "-")
