# Changelog

## 1.2.38 - 2026-11-06

### Added

- Added Home Assistant entities for raw battery ADC level and low-battery
  state.
- Added decoding for the little-endian battery field in status bytes 10-11
  and the 9 V/18 V input class in status byte 1.
- Added controller diagnostics for rain-off readback, valve count, supply
  type, signal strength, status byte 14, and accessory status flags. Fields
  without locally verified semantics are disabled by default.
- Documented live battery samples and the remaining threshold-validation
  caveat.
- Thanks to [@Mitamar06](https://github.com/Mitamar06) for contributing the
  battery and additional status-field decoding in
  [issue #1](https://github.com/nightime7/py-galcon-bt-control/issues/1).

### Fixed

- Encoded manual run durations as hours/minutes/seconds in command bytes 3-5
  so durations over 59 minutes no longer wrap into a single minutes byte.
- Synchronized rain-off days from confirmed status byte 12 readback.

## 1.2.37 - 2026-11-06

### Added

- Added a Home Assistant seasonal status switch that can disable adjustment
  by staging the neutral `100%` value.
- Added seasonal percentage read-back from controller status byte 16.
- Added a seasonal enabled checkbox to the desktop GUI.
- Added device-level Apply and Discard actions for staged seasonal and
  rain-off changes.

### Fixed

- Matched the controller's seasonal semantics: `100%` reports disabled and
  every other percentage reports enabled, with percentage as the only value
  written to the controller.
- Limited Home Assistant seasonal adjustment to verified 10% increments.

## 1.2.36 - 2026-10-20

### Added

- Added a Home Assistant manual-duration number for each zone.
- Added MQTT topics at `zone/{zone}/manual_duration/set` for configuring how
  long a plain `ON` command should water.
- Added logging of the resolved manual watering duration.

### Fixed

- Fixed Home Assistant manual zone starts always watering for one minute.
- Added validation for manual watering durations from 1 through 255 minutes.

## 1.2.35 - 2026-09-05

### Added

- Added staged Home Assistant controls for program duration, weekdays, and
  four start windows per zone.
- Added per-zone Apply and Discard actions for schedule changes.