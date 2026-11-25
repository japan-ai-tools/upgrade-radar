"""A tiny app written against packaging 21.x. Used by examples/demo.sh."""
from packaging.version import LegacyVersion, parse
from packaging import version

v = version.LegacyVersion("1.0-foo")
print(parse("1.0") > v)
print(version.Version("2.0").major)
