"""The stubbed suite. real_ha runs inside a real Home Assistant and is run on its own."""

# The stubs here replace Home Assistant's modules in sys.modules, so the two suites cannot share a
# process: run tests/real_ha with its own command and requirements.
collect_ignore = ["real_ha"]
