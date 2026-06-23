"""Term extraction and optional domain synonym knowledge.

Two jobs:
  1. Generic term extraction for resumes and job descriptions in any field.
  2. Optional synonym/adjacency expansion for specialized domains, so a resume
     listing "Verilog" still matches a JD that only says "RTL" or "HDL".

Keys are canonical lowercase terms; values are adjacent terms that should count
as (slightly discounted) matches. Expansion is bidirectional at load time.
"""

from __future__ import annotations

import re


# Canonical term -> adjacent/equivalent terms. Curated for hardware/embedded
# matching when those terms appear; not used as a source or field filter.
_SYNONYM_SEED: dict[str, list[str]] = {
    # --- HDL / digital design ---
    "verilog": ["rtl", "hdl", "digital design", "systemverilog", "rtl design"],
    "systemverilog": ["verilog", "rtl", "hdl", "uvm", "digital design"],
    "vhdl": ["rtl", "hdl", "digital design"],
    "rtl": ["verilog", "systemverilog", "vhdl", "hdl", "digital design", "rtl design"],
    "hdl": ["verilog", "systemverilog", "vhdl", "rtl"],
    "digital design": ["rtl", "verilog", "logic design", "asic", "fpga"],
    "logic design": ["digital design", "rtl"],
    "fpga": ["rtl", "verilog", "vhdl", "xilinx", "altera", "intel fpga", "vivado", "quartus", "prototyping"],
    "asic": ["rtl", "soc", "vlsi", "physical design", "synthesis", "tapeout", "digital design"],
    "soc": ["asic", "system on chip", "vlsi", "ip integration"],
    "vlsi": ["asic", "physical design", "cmos", "circuit design", "soc"],
    "uvm": ["systemverilog", "verification", "dv", "design verification", "testbench"],
    "design verification": ["uvm", "verification", "dv", "systemverilog", "testbench"],
    "verification": ["uvm", "design verification", "dv", "testbench", "formal verification"],
    "synthesis": ["asic", "rtl", "design compiler", "logic synthesis"],
    "physical design": ["asic", "vlsi", "place and route", "pnr", "timing closure", "sta"],
    "timing analysis": ["sta", "static timing analysis", "timing closure"],
    "tapeout": ["asic", "physical design", "silicon"],

    # --- EDA tools ---
    "vivado": ["fpga", "xilinx", "vitis"],
    "quartus": ["fpga", "altera", "intel fpga"],
    "cadence": ["eda", "virtuoso", "innovus", "spectre"],
    "synopsys": ["eda", "vcs", "design compiler", "primetime"],
    "modelsim": ["simulation", "hdl", "questasim"],

    # --- embedded / firmware ---
    "embedded systems": ["firmware", "embedded c", "microcontroller", "mcu", "rtos", "bare metal", "embedded software"],
    "firmware": ["embedded systems", "embedded c", "bare metal", "mcu", "drivers", "bootloader"],
    "embedded c": ["firmware", "embedded systems", "c", "bare metal"],
    "rtos": ["freertos", "zephyr", "embedded systems", "real time"],
    "freertos": ["rtos", "embedded systems"],
    "microcontroller": ["mcu", "embedded systems", "arm cortex", "stm32", "arduino", "esp32"],
    "mcu": ["microcontroller", "embedded systems"],
    "arm": ["cortex", "arm cortex", "embedded", "soc"],
    "stm32": ["microcontroller", "arm cortex", "embedded"],
    "device drivers": ["firmware", "drivers", "kernel", "bsp"],
    "bootloader": ["firmware", "embedded systems", "u-boot"],
    "bare metal": ["firmware", "embedded systems", "no os"],

    # --- protocols / interfaces ---
    "i2c": ["spi", "uart", "serial", "embedded protocols"],
    "spi": ["i2c", "uart", "serial", "embedded protocols"],
    "uart": ["i2c", "spi", "serial"],
    "can bus": ["automotive", "canbus", "can", "embedded protocols"],
    "pcie": ["high speed", "serdes", "interconnect"],
    "ddr": ["memory controller", "dram", "high speed"],
    "ethernet": ["networking", "mac", "phy"],
    "usb": ["embedded protocols", "device drivers"],

    # --- signal processing / RF ---
    "dsp": ["signal processing", "digital signal processing", "filters", "fft"],
    "signal processing": ["dsp", "digital signal processing", "fft", "filters", "beamforming"],
    "rf": ["radio frequency", "rf design", "antenna", "wireless", "microwave", "mmwave"],
    "beamforming": ["signal processing", "array processing", "mic array", "phased array", "acoustic"],
    "fft": ["dsp", "signal processing"],
    "communications": ["wireless", "modulation", "ofdm", "5g", "rf"],

    # --- power / analog ---
    "power electronics": ["power supply", "dc-dc", "converter", "inverter", "smps", "bms", "motor control"],
    "analog design": ["analog", "circuit design", "mixed signal", "amplifier", "adc", "dac"],
    "mixed signal": ["analog design", "adc", "dac", "soc"],
    "bms": ["battery management", "battery", "power electronics"],
    "motor control": ["power electronics", "foc", "bldc", "inverter"],
    "adc": ["data converter", "mixed signal", "analog"],
    "dac": ["data converter", "mixed signal", "analog"],

    # --- PCB / hardware ---
    "pcb design": ["pcb", "schematic", "layout", "altium", "kicad", "eagle", "hardware design"],
    "pcb": ["pcb design", "schematic capture", "board design"],
    "altium": ["pcb design", "schematic", "layout"],
    "kicad": ["pcb design", "schematic", "layout"],
    "schematic capture": ["pcb design", "schematic"],
    "hardware design": ["pcb design", "circuit design", "board bring-up"],
    "board bring-up": ["hardware design", "debug", "validation", "bring up"],

    # --- robotics / controls ---
    "robotics": ["ros", "autonomy", "controls", "motion planning", "actuators", "mechatronics"],
    "ros": ["robotics", "ros2", "autonomy"],
    "controls": ["control systems", "pid", "state estimation", "kalman", "robotics"],
    "control systems": ["controls", "pid", "feedback"],
    "mechatronics": ["robotics", "electromechanical", "actuators"],
    "autonomy": ["robotics", "perception", "slam", "self-driving"],
    "slam": ["localization", "perception", "robotics"],

    # --- lab / test ---
    "oscilloscope": ["lab equipment", "debug", "test", "logic analyzer"],
    "logic analyzer": ["debug", "lab equipment", "oscilloscope"],
    "lab equipment": ["oscilloscope", "multimeter", "test", "characterization"],
    "characterization": ["validation", "bench test", "silicon validation"],

    # --- general SW that hardware folks use ---
    "c": ["c++", "embedded c", "firmware"],
    "c++": ["c", "embedded"],
    "python": ["scripting", "automation", "numpy"],
    "matlab": ["simulink", "signal processing", "modeling"],
    "simulink": ["matlab", "modeling", "controls"],
    "tcl": ["eda scripting", "automation"],
    "git": ["version control"],
    "linux": ["embedded linux", "yocto", "bsp"],
}


# Terms that are "tools/languages" — these get a higher match weight than generic
# concept terms (spec Section 7: weight exact tool/language matches higher).
TOOLS_AND_LANGUAGES: set[str] = {
    "verilog", "systemverilog", "vhdl", "c", "c++", "python", "matlab", "simulink",
    "tcl", "perl", "rust", "assembly", "vivado", "quartus", "cadence", "synopsys",
    "modelsim", "questasim", "altium", "kicad", "eagle", "spice", "ltspice",
    "freertos", "zephyr", "ros", "ros2", "git", "linux", "stm32", "arduino",
    "esp32", "fpga", "verilator", "vcs", "primetime", "design compiler", "innovus",
    "virtuoso", "spectre", "hfss", "ansys", "comsol", "labview",
}


# A flat lexicon of every term we know how to recognize in free text. Used by the
# resume parser and JD requirement extractor. Multi-word terms are matched first.
TECH_LEXICON: set[str] = set(_SYNONYM_SEED) | TOOLS_AND_LANGUAGES | {
    "machine learning", "deep learning", "computer vision", "edge ai", "tinyml",
    "fpga prototyping", "emulation", "low power", "clock domain crossing", "cdc",
    "lint", "scan", "dft", "jtag", "boundary scan", "serdes", "phy", "high speed",
    "thermal", "emi", "emc", "signal integrity", "power integrity", "ibis",
    "verification ip", "assertion", "coverage", "regression", "constrained random",
    "yocto", "buildroot", "petalinux", "memory controller", "cache", "pipeline",
    "risc-v", "arm cortex", "x86", "gpu", "accelerator", "neural network",
    "sensor fusion", "imu", "lidar", "radar", "camera", "actuator", "servo",
    "5g", "ofdm", "modulation", "wireless", "bluetooth", "ble", "zigbee", "lora",
}


def _normalize(term: str) -> str:
    return re.sub(r"\s+", " ", term.strip().lower())


def _build_bidirectional(seed: dict[str, list[str]]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for k, vs in seed.items():
        k = _normalize(k)
        graph.setdefault(k, set())
        for v in vs:
            v = _normalize(v)
            graph[k].add(v)
            graph.setdefault(v, set()).add(k)
    # remove self-loops
    for k in graph:
        graph[k].discard(k)
    return graph


SYNONYMS: dict[str, set[str]] = _build_bidirectional(_SYNONYM_SEED)


def expand_terms(terms: set[str]) -> dict[str, float]:
    """Expand a set of resume terms into a weighted keyword map.

    Exact resume terms get weight 1.0 (tools/languages 1.3). One-hop synonyms get
    0.6 so adjacent terminology still counts but ranks below exact overlap.
    """
    weights: dict[str, float] = {}
    for raw in terms:
        t = _normalize(raw)
        if not t:
            continue
        base = 1.3 if t in TOOLS_AND_LANGUAGES else 1.0
        weights[t] = max(weights.get(t, 0.0), base)
        for syn in SYNONYMS.get(t, ()):  # one hop only — avoids drift
            syn_w = 0.6 * (1.3 if syn in TOOLS_AND_LANGUAGES else 1.0)
            weights[syn] = max(weights.get(syn, 0.0), syn_w)
    return weights


# Sorted longest-first so multi-word terms win over their substrings during
# whole-text scanning.
_LEXICON_SORTED = sorted(TECH_LEXICON, key=len, reverse=True)
_LEXICON_PATTERNS = [
    (term, re.compile(r"(?<![A-Za-z0-9+#])" + re.escape(term) + r"(?![A-Za-z0-9+#])", re.IGNORECASE))
    for term in _LEXICON_SORTED
]


def extract_known_terms(text: str) -> list[str]:
    """Return known tech terms found in free text, de-duplicated, order-preserved."""
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for term, pat in _LEXICON_PATTERNS:
        if pat.search(text):
            if term not in seen:
                seen.add(term)
                found.append(term)
    return found


# ---------------------------------------------------------------------------
# Field-AGNOSTIC extraction. This lets the tool match any field by pulling
# meaningful terms straight out of the resume text instead of a fixed vocabulary.
# Used in addition to extract_known_terms so specialized synonym expansion still
# works when those terms appear.
# ---------------------------------------------------------------------------

# Common English + resume-boilerplate words we never want as "skills".
_STOPWORDS: set[str] = {
    "the", "and", "for", "with", "from", "that", "this", "these", "those", "into",
    "your", "you", "our", "their", "his", "her", "its", "are", "was", "were", "been",
    "being", "have", "has", "had", "will", "would", "should", "could", "can", "may",
    "all", "any", "each", "more", "most", "some", "such", "than", "then", "them",
    "they", "what", "when", "which", "while", "who", "whom", "how", "why", "where",
    "over", "under", "between", "across", "within", "about", "above", "below", "after",
    "before", "during", "out", "off", "per", "via", "etc", "using", "used", "use",
    "including", "include", "included", "based", "various", "multiple", "several",
    "new", "strong", "excellent", "proven", "ability", "skills", "skill", "experience",
    "experienced", "knowledge", "proficient", "proficiency", "familiar", "working",
    "work", "worked", "team", "teams", "project", "projects", "responsible", "role",
    "roles", "company", "companies", "industry", "year", "years", "month", "months",
    "summer", "fall", "spring", "winter", "intern", "internship", "students", "student",
    "university", "college", "school", "gpa", "degree", "bachelor", "master", "current",
    "present", "developed", "developing", "create", "created", "creating", "build",
    "built", "building", "design", "designed", "designing", "manage", "managed",
    "managing", "lead", "led", "leading", "support", "supported", "help", "helped",
    "provide", "provided", "ensure", "ensured", "improve", "improved", "increase",
    "increased", "reduce", "reduced", "implement", "implemented", "perform", "performed",
    "conduct", "conducted", "assist", "assisted", "collaborate", "collaborated",
    "responsibilities", "duties", "tasks", "various", "different", "good", "great",
    "high", "low", "key", "core", "main", "also", "well", "very", "much", "many",
    "one", "two", "three", "first", "second", "third", "data", "system", "systems",
    "process", "processes", "tools", "tool", "technologies", "technology", "software",
    "hardware", "application", "applications", "solution", "solutions", "environment",
    "email", "phone", "linkedin", "github", "http", "https", "www", "com", "org",
}

_GEN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z+#.&/-]{1,29}")


def _gen_tokens(text: str) -> list[str]:
    toks: list[str] = []
    for raw in _GEN_WORD_RE.findall(text.lower()):
        t = raw.strip(".-/&").strip()
        if len(t) < 3 or t in _STOPWORDS or t.isdigit():
            continue
        toks.append(t)
    return toks


def extract_generic_terms(text: str, *, top_n: int = 40) -> dict[str, float]:
    """Field-agnostic keyword map from arbitrary resume/JD text.

    Returns ``term -> frequency-ish weight`` for the most salient unigrams and
    adjacent bigrams (e.g. "financial modeling", "patient care", "graphic design").
    Weights are modest (≈0.4–0.9) so that, when present, curated domain terms and
    the user's stated target role still outrank generic noise.
    """
    if not text:
        return {}
    counts: dict[str, int] = {}
    # Process line by line so bigrams don't cross unrelated lines/bullets.
    for line in text.splitlines():
        toks = _gen_tokens(line)
        for w in toks:
            counts[w] = counts.get(w, 0) + 1
        for a, b in zip(toks, toks[1:]):
            bigram = f"{a} {b}"
            counts[bigram] = counts.get(bigram, 0) + 1

    # Keep the most frequent terms; bias toward multi-word phrases (more specific).
    ranked = sorted(
        counts.items(),
        key=lambda kv: (kv[1] + (0.5 if " " in kv[0] else 0.0)),
        reverse=True,
    )[:top_n]
    weights: dict[str, float] = {}
    for term, c in ranked:
        w = 0.45 + 0.12 * min(c, 4) + (0.12 if " " in term else 0.0)
        weights[term] = round(min(w, 0.95), 3)
    return weights
