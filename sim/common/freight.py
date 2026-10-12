"""What physically happens to a store delivery between Corp A's dock and the store's back room.

Corp A (which loads the truck) and the client's store (which unloads it) both need the same truth about each
truckload, and neither tells the control tower that truth directly. The tower sees only what real parties
would say: the ship notice, the carrier's note on the proof of delivery, the warehouse's next physical count,
and the store's receiving report.

The outcome is a pure function of the ship notice, so both simulators agree without talking to each other:

  mis_pick        one case of an item was swapped for a case of another item on the same order
  short_pick      one case on the notice never left the warehouse shelf
  carrier_damage  one case arrived crushed; the driver noted it on the proof of delivery
  carrier_loss    one case went missing in the carrier's network; usually noted on the proof of delivery
"""
from __future__ import annotations

import random
from typing import Any

TRUCK_FATES = (("mis_pick", 0.02), ("short_pick", 0.025), ("carrier_damage", 0.025), ("carrier_loss", 0.012))
POD_NOTED_LOSS = 0.85         # a missing case the driver noted at the door; the rest only show up in the count
CARRIER_FATES = ("carrier_damage", "carrier_loss")


def truck_outcome(asn_id: str, lines: list[dict[str, Any]],
                  fates: tuple[tuple[str, float], ...] = TRUCK_FATES) -> dict[str, Any]:
    """`lines` are the ship notice's lines (sellerSku, unitsShipped, casePack). Returns the fate, the item it
    hit, what was loaded, what arrived (damaged cases included) and what arrived damaged, by SKU, and the
    carrier's note on the proof of delivery (None when the delivery was signed clean)."""
    rng = random.Random(f"truck:{asn_id}")
    lines = sorted((ln for ln in lines if int(ln.get("unitsShipped") or 0) > 0), key=lambda ln: ln["sellerSku"])
    shipped = {ln["sellerSku"]: int(ln["unitsShipped"]) for ln in lines}
    pack = {ln["sellerSku"]: max(1, int(ln.get("casePack") or 1)) for ln in lines}
    r, fate = rng.random(), "normal"
    for name, p in fates:
        if r < p:
            fate = name
            break
        r -= p
    out = {"fate": fate, "sku": None, "other_sku": None, "loaded": dict(shipped), "arrived": dict(shipped),
           "damaged": {}, "pod_note": None}
    if fate == "normal" or not lines:
        out["fate"] = "normal"
        return out
    sku = rng.choice(lines)["sellerSku"]
    out["sku"] = sku
    others = [ln["sellerSku"] for ln in lines if ln["sellerSku"] != sku]
    pick_other = rng.choice(others) if others else None
    noted = rng.random() < POD_NOTED_LOSS
    if fate == "mis_pick" and pick_other is None:
        fate = out["fate"] = "short_pick"     # nothing else on the order to grab by mistake
    if fate == "mis_pick":
        out["other_sku"] = pick_other
        out["loaded"][sku] -= pack[sku]
        out["loaded"][pick_other] += pack[pick_other]
    elif fate == "short_pick":
        out["loaded"][sku] -= pack[sku]
    out["arrived"] = dict(out["loaded"])
    cases = sum(shipped[s] // pack[s] for s in shipped)
    if fate == "carrier_damage":
        out["damaged"] = {sku: pack[sku]}
        out["pod_note"] = "Delivered; 1 case damaged (crushed carton), exception noted on POD by driver"
    elif fate == "carrier_loss":
        out["arrived"][sku] -= pack[sku]
        if noted:
            out["pod_note"] = f"Delivered short: {cases - 1} of {cases} cases received, exception noted on POD"
    return out
