"""Score a scanned netlist against a reference netlist (e.g. the EDA tool's own export of the same design).

Connections are compared as pairs of pins on one net: precision is the share of the scan's pin pairs that the
reference has too, recall the share of the reference's pairs the scan found. Two-pin parts whose pin numbers were
guessed are matched in whichever orientation agrees with the reference.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from ..kicad_netlist import Netlist


@dataclass
class Score:
    components_reference: int
    components_found: int
    components_missing: list[str] = field(default_factory=list)
    components_extra: list[str] = field(default_factory=list)
    pairs_reference: int = 0
    pairs_scan: int = 0
    pairs_common: int = 0
    nets_exact: int = 0
    nets_reference: int = 0
    values_matching: int = 0

    @property
    def precision(self) -> float:
        return self.pairs_common / self.pairs_scan if self.pairs_scan else 0.0

    @property
    def recall(self) -> float:
        return self.pairs_common / self.pairs_reference if self.pairs_reference else 0.0

    def summary(self) -> str:
        return (f"components {self.components_found}/{self.components_reference} found"
                f" ({len(self.components_extra)} extra), values {self.values_matching} matching;"
                f" pin connections precision {self.precision:.1%} recall {self.recall:.1%};"
                f" nets identical {self.nets_exact}/{self.nets_reference}")


def _pairs(net_of: dict) -> set:
    members = defaultdict(list)
    for node, net in net_of.items():
        members[net].append(node)
    pairs = set()
    for nodes in members.values():
        for a, b in combinations(sorted(nodes), 2):
            pairs.add((a, b))
    return pairs


def _real(ref: str) -> bool:
    return not ref.startswith("#")


def compare(scan: Netlist, reference: Netlist, guessed: set[str] | None = None) -> Score:
    guessed = guessed or set()
    ref_parts = {r for r in reference.components if _real(r)}
    scan_parts = set(scan.components)
    score = Score(components_reference=len(ref_parts), components_found=len(ref_parts & scan_parts),
                  components_missing=sorted(ref_parts - scan_parts), components_extra=sorted(scan_parts - ref_parts))
    score.values_matching = sum(1 for r in ref_parts & scan_parts
                                if scan.components[r].value.strip().lower() ==
                                reference.components[r].value.strip().lower())
    ref_net = {(n.ref, n.pin): i for i, net in enumerate(reference.nets) for n in net.nodes if _real(n.ref)}
    scan_net = {(n.ref, n.pin): i for i, net in enumerate(scan.nets) for n in net.nodes}

    # orient two-pin parts with guessed numbers the way the reference agrees with
    pins_of = defaultdict(set)
    for ref, pin in scan_net:
        pins_of[ref].add(pin)
    for ref, pins in pins_of.items():
        if pins != {"1", "2"} or not ({f"{ref}.1", f"{ref}.2"} & guessed):
            continue
        a, b = scan_net[(ref, "1")], scan_net[(ref, "2")]
        others = defaultdict(set)
        for node, net in scan_net.items():
            if node[0] != ref:
                others[net].add(node)

        def agree(n1, n2):
            total = 0
            for pin, net in (("1", n1), ("2", n2)):
                target = ref_net.get((ref, pin))
                total += sum(1 for node in others[net] if ref_net.get(node) == target and target is not None)
            return total
        if agree(b, a) > agree(a, b):
            scan_net[(ref, "1")], scan_net[(ref, "2")] = b, a

    ref_pairs = _pairs({k: v for k, v in ref_net.items()})
    scan_pairs = _pairs(scan_net)
    score.pairs_reference = len(ref_pairs)
    score.pairs_scan = len(scan_pairs)
    score.pairs_common = len(ref_pairs & scan_pairs)
    ref_members = defaultdict(set)
    for node, net in ref_net.items():
        ref_members[net].add(node)
    scan_members = defaultdict(set)
    for node, net in scan_net.items():
        scan_members[net].add(node)
    scan_sets = {frozenset(v) for v in scan_members.values()}
    multi = [v for v in ref_members.values() if len(v) > 1]
    score.nets_reference = len(multi)
    score.nets_exact = sum(1 for v in multi if frozenset(v) in scan_sets)
    return score
