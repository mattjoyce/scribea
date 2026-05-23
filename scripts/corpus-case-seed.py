#!/usr/bin/env python3
"""Roll a randomised ICU case seed and write a case.yaml stub.

Outputs:
  1. A JSON seed to stdout (the rolled fields, for inspection or piping).
  2. (Unless --no-write) a case.yaml stub at
     test-corpus/cases/<case_id>/case.yaml with demographics +
     admission_reason + audio_render config pre-filled.

The corpus author then fills in previous_notes, the clinician name, and
the per-clip dictation scripts in scripts/NN.txt — see
docs/scribe-test-corpus.md for the full case.yaml shape and
docs/icu-notes-two-approaches.md for the dictation styles by role.

Examples
--------
    ./scripts/corpus-case-seed.py                    # roll one case
    ./scripts/corpus-case-seed.py --seed 42          # reproducible roll
    ./scripts/corpus-case-seed.py --role vmo         # constrain role
    ./scripts/corpus-case-seed.py --name my_case     # override case_id
    ./scripts/corpus-case-seed.py --no-write         # JSON only
    ./scripts/corpus-case-seed.py --count 5          # batch JSON (no files)

Stdlib only (no click/yaml deps) to keep the script runnable without setup.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import secrets
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Data tables — edit freely to tune the distribution.
# ---------------------------------------------------------------------------

SEXES: list[str] = ["M", "F"]

ETHNICITIES: list[str] = [
    "European Australian",
    "Aboriginal or Torres Strait Islander",
    "East Asian",
    "South Asian",
    "Southeast Asian",
    "Pacific Islander",
    "Middle Eastern",
    "African",
    "Mediterranean European",
    "Latin American",
    "Mixed or other",
]

# Each tuple: (low_inclusive, high_inclusive, weight). ICU skews older.
AGE_BRACKETS: list[tuple[int, int, float]] = [
    (18, 29, 0.05),
    (30, 44, 0.10),
    (45, 59, 0.18),
    (60, 74, 0.30),
    (75, 89, 0.27),
    (90, 99, 0.10),
]

BODY_HABITUS: list[str] = ["slim", "average", "overweight", "obese", "frail elderly"]

# Primary system -> plausible admission reasons. Drives the case's centre of gravity.
ADMISSION_BY_SYSTEM: dict[str, list[str]] = {
    "cardiovascular": [
        "STEMI", "NSTEMI", "cardiogenic shock", "post-cardiac-arrest syndrome",
        "decompensated heart failure", "VT/VF arrest",
    ],
    "respiratory": [
        "community-acquired pneumonia", "COPD exacerbation", "severe asthma",
        "ARDS", "pulmonary embolism", "post-extubation respiratory failure",
    ],
    "gastrointestinal": [
        "upper GI haemorrhage", "lower GI haemorrhage", "acute pancreatitis",
        "perforated viscus", "small-bowel obstruction with ischaemia",
        "acute liver failure",
    ],
    "genitourinary": [
        "urosepsis", "obstructive AKI", "rhabdomyolysis with AKI",
    ],
    "haematological": [
        "febrile neutropenia", "severe anaemia requiring transfusion",
        "thrombotic thrombocytopenic purpura", "disseminated intravascular coagulation",
        "massive transfusion protocol",
    ],
    "rheumatological": [
        "vasculitis flare", "lupus cerebritis", "scleroderma renal crisis",
    ],
    "endocrine": [
        "diabetic ketoacidosis", "hyperosmolar hyperglycaemic state",
        "thyroid storm", "adrenal crisis", "severe symptomatic hyponatraemia",
    ],
    "nervous": [
        "ischaemic stroke with malignant oedema", "intracerebral haemorrhage",
        "aneurysmal subarachnoid haemorrhage", "status epilepticus",
        "Guillain-Barré syndrome", "severe traumatic brain injury",
        "autoimmune encephalitis",
    ],
    "psychiatric": [
        "tricyclic antidepressant overdose", "serotonin syndrome",
        "neuroleptic malignant syndrome",
        "polypharmacy self-poisoning requiring ventilation",
    ],
}
SYSTEMS: list[str] = list(ADMISSION_BY_SYSTEM.keys())

ICU_DAYS: list[tuple[int, float]] = [
    (1, 0.18), (2, 0.18), (3, 0.18), (5, 0.16), (7, 0.12),
    (10, 0.08), (14, 0.06), (21, 0.04),
]

TRAJECTORIES: list[tuple[str, float]] = [
    ("improving", 0.45), ("stable", 0.35), ("deteriorating", 0.20),
]

VENTILATION_SUPPORT: list[tuple[str, float]] = [
    ("intubated", 0.55), ("NIV", 0.15), ("HFNC", 0.15), ("room air", 0.15),
]

VASOACTIVE_SUPPORT: list[tuple[str, float]] = [
    ("none", 0.35),
    ("noradrenaline (low dose)", 0.20),
    ("noradrenaline (weaning)", 0.20),
    ("noradrenaline + vasopressin", 0.15),
    ("noradrenaline + adrenaline + vasopressin", 0.10),
]

RENAL_SUPPORT: list[tuple[str, float]] = [
    ("none", 0.55), ("oliguric AKI", 0.30), ("on CRRT", 0.15),
]

COMORBIDITIES: list[str] = [
    "type 2 diabetes", "hypertension", "dyslipidaemia",
    "ischaemic heart disease", "chronic heart failure",
    "COPD", "asthma", "chronic kidney disease",
    "cirrhosis", "active malignancy", "prior stroke",
    "atrial fibrillation on warfarin", "atrial fibrillation on DOAC",
    "dementia", "transplant immunosuppression",
    "steroid-treated autoimmune disease", "obstructive sleep apnoea",
]

ALLERGIES: list[tuple[str, float]] = [
    ("none documented", 0.65),
    ("penicillin", 0.15),
    ("sulpha drugs", 0.05),
    ("IV contrast", 0.05),
    ("NSAIDs", 0.05),
    ("latex", 0.05),
]

ANTICOAGULATION: list[tuple[str, float]] = [
    ("none", 0.55),
    ("aspirin", 0.15),
    ("aspirin + clopidogrel", 0.05),
    ("apixaban", 0.10),
    ("rivaroxaban", 0.05),
    ("warfarin", 0.05),
    ("enoxaparin (prophylactic)", 0.05),
]

CODE_STATUS: list[tuple[str, float]] = [
    ("For full active treatment", 0.70),
    ("For full ICU treatment, not for CPR", 0.15),
    ("Not for invasive ventilation, ICU-ceiling-of-care", 0.10),
    ("Ward-based ceiling, no escalation", 0.05),
]

ROLES: list[str] = ["vmo", "registrar"]
ROLE_TO_STYLE: dict[str, str] = {"vmo": "systems", "registrar": "checklist"}
ROLE_TO_VOICE: dict[str, str] = {"vmo": "ballad", "registrar": "onyx"}
ROLE_TO_TEMPLATE: dict[str, str] = {"vmo": "icu_vmo", "registrar": "icu_registrar"}

# Short slugs for case_id construction.
SYSTEM_SLUG: dict[str, str] = {
    "cardiovascular": "cvs", "respiratory": "resp", "gastrointestinal": "gi",
    "genitourinary": "gu", "haematological": "heme", "rheumatological": "rheum",
    "endocrine": "endo", "nervous": "neuro", "psychiatric": "psych",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def pick_weighted(rng: random.Random, items: list[tuple[Any, float]]) -> Any:
    """Pick one item from a list of (value, weight) tuples."""
    values = [v for v, _ in items]
    weights = [w for _, w in items]
    return rng.choices(values, weights=weights, k=1)[0]


def slugify(text: str) -> str:
    """Compress arbitrary text into a short kebab-case slug."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    return s[:32]


def keyword_for_reason(reason: str) -> str:
    """First useful word from an admission reason, slug-friendly."""
    words = re.split(r"\W+", reason.lower())
    stop = {"the", "a", "an", "of", "with", "on", "in", "post", "acute", "severe", "and"}
    for w in words:
        if w and w not in stop and len(w) > 2:
            return w[:12]
    return slugify(reason)[:12]


# ---------------------------------------------------------------------------
# Roller
# ---------------------------------------------------------------------------

def roll_case(rng: random.Random, role_hint: str | None = None) -> dict[str, Any]:
    role = role_hint or rng.choice(ROLES)

    sex = rng.choice(SEXES)
    ethnicity = rng.choice(ETHNICITIES)
    bracket = pick_weighted(rng, [((lo, hi), w) for (lo, hi, w) in AGE_BRACKETS])
    age = rng.randint(bracket[0], bracket[1])
    age_bracket_label = f"{bracket[0]}-{bracket[1]}"
    body_habitus = rng.choice(BODY_HABITUS)

    primary_system = rng.choice(SYSTEMS)
    admission_reason = rng.choice(ADMISSION_BY_SYSTEM[primary_system])

    # Optional secondary systems (0-2 distinct).
    n_secondary = rng.choices([0, 1, 2], weights=[0.30, 0.45, 0.25], k=1)[0]
    secondary_pool = [s for s in SYSTEMS if s != primary_system]
    rng.shuffle(secondary_pool)
    secondary_systems = secondary_pool[:n_secondary]

    icu_day = pick_weighted(rng, ICU_DAYS)
    trajectory = pick_weighted(rng, TRAJECTORIES)
    ventilation = pick_weighted(rng, VENTILATION_SUPPORT)
    vasoactive = pick_weighted(rng, VASOACTIVE_SUPPORT)
    renal = pick_weighted(rng, RENAL_SUPPORT)

    # 0–3 comorbidities, weighted toward 1–2.
    n_comorbid = rng.choices([0, 1, 2, 3], weights=[0.15, 0.40, 0.30, 0.15], k=1)[0]
    comorbidities = rng.sample(COMORBIDITIES, k=min(n_comorbid, len(COMORBIDITIES)))

    allergies = pick_weighted(rng, ALLERGIES)
    anticoagulation = pick_weighted(rng, ANTICOAGULATION)
    code_status = pick_weighted(rng, CODE_STATUS)

    sys_slug = SYSTEM_SLUG[primary_system]
    reason_kw = keyword_for_reason(admission_reason)
    case_slug = f"{sys_slug}_{reason_kw}_{role}"

    return {
        "case_slug": case_slug,
        "role": role,
        "template_id": ROLE_TO_TEMPLATE[role],
        "dictation_style": ROLE_TO_STYLE[role],
        "voice_id": ROLE_TO_VOICE[role],
        "demographics": {
            "sex": sex,
            "ethnicity": ethnicity,
            "age": age,
            "age_bracket": age_bracket_label,
            "body_habitus": body_habitus,
            "icu_day": icu_day,
            "admission_reason": admission_reason,
        },
        "clinical": {
            "primary_system": primary_system,
            "secondary_systems": secondary_systems,
            "trajectory": trajectory,
            "ventilation": ventilation,
            "vasoactive": vasoactive,
            "renal": renal,
        },
        "background": {
            "comorbidities": comorbidities,
            "allergies": allergies,
            "anticoagulation": anticoagulation,
            "code_status": code_status,
        },
    }


# ---------------------------------------------------------------------------
# case.yaml stub writer (string templating — avoids a yaml dependency)
# ---------------------------------------------------------------------------

def yaml_str_block(text: str, indent: int = 2) -> str:
    """Render a multi-line string as a YAML block scalar (`|`)."""
    pad = " " * indent
    lines = text.rstrip().split("\n") if text else ["TODO"]
    return "|\n" + "\n".join(f"{pad}{ln}" for ln in lines)


def yaml_str_quoted(text: str) -> str:
    """Double-quote a YAML string, escaping internal quotes."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_case_yaml(case_id: str, seed: dict[str, Any], rng_seed: int | None) -> str:
    demo = seed["demographics"]
    clinical = seed["clinical"]
    bg = seed["background"]
    role = seed["role"]

    # Seed metadata block — full rolled JSON as a YAML scalar for a permanent
    # record of how the case was generated. The author may delete this once
    # the case is fleshed out, but keeping it preserves reproducibility.
    seed_meta_json = json.dumps(
        {**seed, "rng_seed": rng_seed},
        indent=2, ensure_ascii=False,
    )

    previous_notes_todo = (
        f"TODO: write a paragraph of plausible previous notes consistent with the\n"
        f"seed above. Include the admission story (presenting features, how the\n"
        f"patient got to ICU), current treatments matching the rolled supports\n"
        f"(ventilation: {clinical['ventilation']}; vasoactive: {clinical['vasoactive']};\n"
        f"renal: {clinical['renal']}), and any relevant recent events.\n"
        f"Reference the comorbidities ({', '.join(bg['comorbidities']) or 'nil significant'}),\n"
        f"allergies ({bg['allergies']}), and code status ({bg['code_status']}) as\n"
        f"appropriate."
    )

    lines: list[str] = []
    lines.append(f"# Auto-generated stub by scripts/corpus-case-seed.py")
    lines.append(f"# TODO: fill in display_name, previous_notes, clinician, scripts/NN.txt")
    lines.append("")
    lines.append("# _seed records the rolled fields so the case is reproducible and the")
    lines.append("# author has the full clinical scenario as context when writing scripts.")
    lines.append("_seed: " + yaml_str_block(seed_meta_json, indent=2))
    lines.append("")
    lines.append(f"case_id: {case_id}")
    lines.append("")
    lines.append("demographics:")
    lines.append(f"  display_name: {yaml_str_quoted('(faux) TODO')}")
    lines.append(f"  age: {demo['age']}")
    lines.append(f"  sex: {demo['sex']}")
    lines.append(f"  icu_day: {demo['icu_day']}")
    lines.append(f"  admission_reason: {yaml_str_quoted(demo['admission_reason'])}")
    lines.append("")
    lines.append("previous_notes: " + yaml_str_block(previous_notes_todo, indent=2))
    lines.append("")
    lines.append("session:")
    lines.append(f"  role: {role}")
    lines.append(f"  dictation_style: {seed['dictation_style']}")
    lines.append(f"  clinician: {yaml_str_quoted('TODO Dr A.Smith')}")
    lines.append("  audio_render:")
    lines.append(f"    voice_id: {seed['voice_id']}")
    lines.append("    background_pool: icu_ambient")
    lines.append("    snr_db: -18")
    lines.append(f"    seed: {case_id}")
    lines.append("  clips:")
    lines.append("    - audio:  clips/01.mp3")
    lines.append("      script: scripts/01.txt")
    lines.append("    - audio:  clips/02.mp3")
    lines.append("      script: scripts/02.txt")
    lines.append("    - audio:  clips/03.mp3")
    lines.append("      script: scripts/03.txt")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Roll a randomised ICU case seed and (optionally) write a case.yaml stub.",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="RNG seed for reproducible rolls. If omitted, a random seed is used.",
    )
    parser.add_argument(
        "--role", choices=ROLES, default=None,
        help="Constrain the role to vmo or registrar. Default: random.",
    )
    parser.add_argument(
        "--name", default=None,
        help="Override the auto-generated case_id (default: <system>_<reason>_<role>).",
    )
    parser.add_argument(
        "--count", type=int, default=1,
        help="Number of cases to roll. For count>1, only JSON is printed (no files written).",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=REPO_ROOT / "test-corpus" / "cases",
        help="Directory under which to create the case folder. Default: test-corpus/cases/",
    )
    parser.add_argument(
        "--no-write", action="store_true",
        help="Print JSON only — do not write case.yaml stub.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing case.yaml stub. Default: refuse if it exists.",
    )
    args = parser.parse_args()

    if args.count < 1:
        parser.error("--count must be >= 1")
    if args.count > 1 and args.name:
        parser.error("--name cannot be used with --count > 1")

    # Resolve RNG seed. If unspecified, mint a random one and surface it so
    # the user can reproduce this roll later.
    rng_seed = args.seed if args.seed is not None else secrets.randbits(31)
    rng = random.Random(rng_seed)

    rolls: list[dict[str, Any]] = []
    for _ in range(args.count):
        case = roll_case(rng, role_hint=args.role)
        rolls.append(case)

    # Stdout payload: single object for count==1, list otherwise.
    stdout_payload = rolls[0] if args.count == 1 else rolls
    # Annotate with rng_seed so the surface JSON also captures it.
    if isinstance(stdout_payload, dict):
        stdout_payload = {"rng_seed": rng_seed, **stdout_payload}
    else:
        stdout_payload = {"rng_seed": rng_seed, "cases": stdout_payload}
    print(json.dumps(stdout_payload, indent=2, ensure_ascii=False))

    if args.no_write or args.count > 1:
        return 0

    seed = rolls[0]
    case_id = args.name or seed["case_slug"]
    case_dir = args.out_dir / case_id
    case_yaml_path = case_dir / "case.yaml"

    if case_yaml_path.exists() and not args.force:
        print(
            f"\nrefusing to overwrite existing {case_yaml_path} (use --force)",
            file=sys.stderr,
        )
        return 1

    case_dir.mkdir(parents=True, exist_ok=True)
    case_yaml_path.write_text(render_case_yaml(case_id, seed, rng_seed) + "\n", encoding="utf-8")
    print(f"\nwrote {case_yaml_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
