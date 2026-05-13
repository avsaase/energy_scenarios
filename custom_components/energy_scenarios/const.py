"""Constants for Energy Scenarios."""

DOMAIN = "energy_scenarios"

# Config keys — energy sensors
GRID_IMPORT_SENSOR = "grid_import_sensor"
GRID_EXPORT_SENSOR = "grid_export_sensor"
SOLAR_PRODUCTION_SENSOR = "solar_production_sensor"
BATTERY_CHARGE_SENSOR = "battery_charge_sensor"
BATTERY_DISCHARGE_SENSOR = "battery_discharge_sensor"

# Config keys — price sensors
TAKE_PRICE_SENSOR = "take_price_sensor"
FEED_PRICE_SENSOR = "feed_price_sensor"

# Services
SERVICE_RESET_COST = "reset_cost"
SERVICE_CALIBRATE = "calibrate"

# Intervals
QUARTERLY = "quarterly"
HOURLY = "hourly"
DAILY = "daily"
WEEKLY = "weekly"
MONTHLY = "monthly"
YEARLY = "yearly"
MANUAL = "manual"

INTERVALS = [QUARTERLY, HOURLY, DAILY, WEEKLY, MONTHLY, YEARLY, MANUAL]

SELECTED_SENSORS = "selected_sensors"

SENSOR_LABELS = {
    QUARTERLY: "15-Minute Cost",
    HOURLY: "Hourly Cost",
    DAILY: "Daily Cost",
    WEEKLY: "Weekly Cost",
    MONTHLY: "Monthly Cost",
    YEARLY: "Yearly Cost",
    MANUAL: "Manual Cost (no automatic reset)",
}
