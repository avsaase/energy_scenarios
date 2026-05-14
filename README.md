# Energy Scenarios

A Home Assistant integration that calculates what your energy bill would have been without your battery or solar installation.

## How it works

You point it at your grid, battery, and solar energy sensors. It computes net energy cost for three scenarios:

- **Real** — your actual flows
- **No battery** — solar still running, battery removed
- **Baseline** — no battery, no solar

From these it derives savings sensors:

- **Battery savings** — real vs. no-battery cost
- **Solar savings** — no-battery vs. baseline cost
- **Total savings** — real vs. baseline cost

Each sensor is available per interval: 15-minute, hourly, daily, weekly, monthly, yearly, and manual reset.

Costs are calculated on each energy sensor update using the current price, so dynamic tariffs (Tibber, Nord Pool, etc.) work correctly.

## What you need

| Sensor | Required |
|---|---|
| Grid import (kWh) | Yes |
| Electricity take price | Yes |
| Grid export (kWh) | No |
| Solar production (kWh) | No |
| Battery charge (kWh) | No |
| Battery discharge (kWh) | No |
| Feed-in price | No |

Battery charge and discharge must both be provided or neither.

## Installation

Add this repository as a custom integration in HACS. After restarting Home Assistant, add the integration from **Settings → Devices & Services → Add Integration** and search for "Energy Scenarios".

## Services

**`energy_scenarios.reset_cost`** — reset a cost sensor to zero.

**`energy_scenarios.calibrate`** — set a cost sensor to a specific value.
