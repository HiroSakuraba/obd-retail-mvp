"""Mock fitment / catalog resolver (MVP).

The diagnostic graphs output repair ACTIONS (e.g. replace_intake_pcv_hose),
never SKUs. This module is the separate layer that turns

    repair_action + VIN/year/make/model/engine + retailer

into a buyable, vehicle-specific part: SKU, price, stock, tools, guide,
video. In production this is the retailer catalog adapter (AutoZone,
O'Reilly, ...); here it is a small mock catalog so the vertical slice
(diagnosis -> repair action -> fitment -> SKU) can be exercised end to end.

Fitment, emissions compliance (e.g. California CARB-approved converters),
and pricing live HERE, not in the diagnostic graphs.
"""
from __future__ import annotations


class UnknownRepairAction(Exception):
    pass


class NoFitment(Exception):
    pass


# Mock VIN-prefix -> vehicle description. A production resolver decodes the
# full VIN (year/make/model/engine/drivetrain/emissions); the mock only
# needs enough to route the demo vehicles.
VIN_PREFIXES = {
    "1FMCU": {"year": 2018, "make": "Ford", "model": "Escape",
              "engine": "1.5L", "vehicle": "2018 Ford Escape 1.5L"},
    "2HGFC": {"year": 2016, "make": "Honda", "model": "Civic",
              "engine": "2.0L", "vehicle": "2016 Honda Civic 2.0L"},
    "4T1BF": {"year": 2015, "make": "Toyota", "model": "Camry",
              "engine": "2.5L", "vehicle": "2015 Toyota Camry 2.5L"},
    "2HKRW": {"year": 2014, "make": "Honda", "model": "CR-V",
              "engine": "2.4L", "vehicle": "2014 Honda CR-V 2.4L"},
    "1FTEW": {"year": 2017, "make": "Ford", "model": "F-150",
              "engine": "3.5L", "vehicle": "2017 Ford F-150 3.5L"},
    "2T1BU": {"year": 2013, "make": "Toyota", "model": "Corolla",
              "engine": "1.8L", "vehicle": "2013 Toyota Corolla 1.8L"},
}


def _part(sku, price_usd, stock="in_stock", guide=None, video=None,
          emissions_note=None):
    return {"sku": sku, "price_usd": price_usd, "stock": stock,
            "guide_url": guide or f"https://example.com/guides/{sku.lower()}",
            "video_url": video or f"https://example.com/videos/{sku.lower()}",
            "emissions_note": emissions_note}


# repair_action -> {"fits": {vehicle: part}, "default": part}.
# Prices/stock are mock; the shape is the contract.
CATALOG = {
    "replace_intake_pcv_hose": {
        "fits": {
            "2018 Ford Escape 1.5L": _part("HOSE-INTAKE-ESC15", 24.99),
            "2017 Ford F-150 3.5L": _part("HOSE-PCV-F15035", 19.99),
        },
        "default": _part("HOSE-PCV-UNIV", 16.99),
    },
    "service_maf_sensor": {
        "fits": {
            "2018 Ford Escape 1.5L": _part("MAF-CLEANER", 9.99),
        },
        "default": _part("MAF-CLEANER", 9.99),
    },
    "replace_ignition_coil": {
        "fits": {
            "2016 Honda Civic 2.0L": _part("COIL-CIVIC20", 64.99),
            "2013 Toyota Corolla 1.8L": _part("COIL-COROLLA18", 54.99),
        },
        "default": _part("COIL-UNIV", 59.99),
    },
    "replace_spark_plugs": {
        "fits": {
            "2016 Honda Civic 2.0L": _part("PLUG-IRIDIUM-4PK", 39.99),
        },
        "default": _part("PLUG-IRIDIUM-4PK", 39.99),
    },
    "replace_thermostat": {
        "fits": {
            "2015 Toyota Camry 2.5L": _part("THERM-192F-CAMRY", 18.99),
        },
        "default": _part("THERM-192F", 18.99),
    },
    "replace_ect_sensor": {
        "fits": {},
        "default": _part("ECT-SENSOR", 16.99),
    },
    "replace_catalytic_converter_bank1": {
        "fits": {
            "2014 Honda CR-V 2.4L": _part(
                "CAT-CRV24-FED", 849.99, stock="ships_2_day",
                emissions_note="Federal-EPA converter. NOT legal for sale or "
                               "installation on California-registered "
                               "vehicles: CARB requires an aftermarket "
                               "converter approved for the specific "
                               "vehicle/engine test group (see CARB "
                               "aftermarket catalytic converter database)."),
        },
        "default": _part(
            "CAT-UNIV-FED", 799.99, stock="ships_2_day",
            emissions_note="Federal-EPA converter. Verify CARB compliance "
                           "before sale/installation in California."),
    },
    "replace_downstream_o2_sensor": {
        "fits": {
            "2014 Honda CR-V 2.4L": _part("O2-DOWN-CRV24", 59.99),
        },
        "default": _part("O2-DOWN-UNIV", 54.99),
    },
    "repair_exhaust_leak": {
        "fits": {},
        "default": _part("EXH-GASKET-KIT", 29.99),
    },
    "replace_gas_cap": {
        "fits": {
            "2017 Ford F-150 3.5L": _part("GASCAP-F150", 19.99),
        },
        "default": _part("GASCAP-OE-STYLE", 17.99),
    },
    "repair_evap_hose": {
        "fits": {},
        "default": _part("EVAP-HOSE-KIT", 34.99),
    },
    "replace_vent_valve": {
        "fits": {
            "2017 Ford F-150 3.5L": _part("VENT-VALVE-F150", 44.99),
        },
        "default": _part("VENT-VALVE-UNIV", 39.99),
    },
    "replace_upstream_o2_sensor": {
        "fits": {
            "2013 Toyota Corolla 1.8L": _part("O2-UP-COROLLA18", 74.99),
        },
        "default": _part("O2-UP-UNIV", 69.99),
    },
    "repair_manifold_gasket": {
        "fits": {},
        "default": _part("MANIFOLD-GASKET", 21.99),
    },
    "repair_o2_wiring": {
        "fits": {},
        "default": _part("O2-PIGTAIL", 14.99),
    },
}


def decode_vehicle(vin: str | None) -> dict | None:
    """Mock VIN decode: prefix table only. Returns None when unknown."""
    if not vin:
        return None
    vin = vin.strip().upper()
    for prefix, info in VIN_PREFIXES.items():
        if vin.startswith(prefix):
            return dict(info)
    return None


def resolve_fitment(repair_action: str, vin: str | None = None,
                    year: int | None = None, make: str | None = None,
                    model: str | None = None, engine: str | None = None,
                    state: str | None = None) -> dict:
    """Resolve a repair action to a buyable part for one vehicle.

    Returns the fitment record; raises UnknownRepairAction / NoFitment.
    diagnostic-class actions (inspect/test) have no parts by design.
    """
    entry = CATALOG.get(repair_action)
    if entry is None:
        raise UnknownRepairAction(repair_action)

    vehicle = None
    if year and make and model:
        vehicle = f"{year} {make} {model}" + (f" {engine}" if engine else "")
        vehicle = vehicle.strip()
    elif vin:
        info = decode_vehicle(vin)
        if info:
            vehicle = info["vehicle"]

    part = entry["fits"].get(vehicle) if vehicle else None
    fitment_confirmed = part is not None
    if part is None:
        part = entry["default"]

    record = {
        "repair_action": repair_action,
        "vehicle": vehicle,
        "fitment_confirmed": fitment_confirmed,
        **part,
    }
    if not fitment_confirmed:
        record["fitment_note"] = (
            "No exact-fit catalog entry for this vehicle in the mock "
            "catalog; verify fitment before purchase.")
    # California catalytic-converter compliance is a fitment concern, not
    # a diagnostic one: flag it here where the SKU is chosen.
    if (repair_action == "replace_catalytic_converter_bank1"
            and (state or "").upper() in ("CA", "CALIFORNIA")):
        record["emissions_note"] = (
            "CALIFORNIA: this federal converter must NOT be sold for a "
            "CA-registered vehicle. Substitute a CARB-approved aftermarket "
            "converter listed for this exact engine test group.")
        record["stock"] = "not_available_ca"
    return record
