"""Deterministic, structural/extractive Context Package projection (M3.3).

WHAT THIS DOES

Turns a governed Context Package into the smallest payload a worker can
usefully act on, under a token budget, by SELECTING and OMITTING existing
material. Every character in a payload appeared verbatim in the package.

WHAT THIS MUST NEVER DO

No LLM summarization, no paraphrase, no inferred relevance, no invented fact.
Projection may not upgrade a `load_mode`, turn METADATA_ONLY into FULL_BODY,
resolve an ambiguity, reinterpret or suppress a diagnostic, read the project,
or bypass M2 selection. It reduces information; it never increases authority.

INPUT BOUNDARY

The projector consumes a Context Package (the object M2 emits, or its dict
form). It never touches `.ai/`, never opens a file, and never calls a model.
The canonical flow is:

    .ai/ -> M2 loader -> Context Package -> projector -> payload -> worker

NOT-READY PACKAGES

BLOCKED and DIAGNOSTIC_ONLY packages are not worker-ready and yield a
structured refusal, not an exception and not a partial payload.
"""

from __future__ import annotations

from typing import Any

from .budget import BudgetProfile, compact, measure, resolve_profile
from .skeleton import generate_skeleton
from .model import (BUDGET_OVERFLOW, MISSING_REQUIRED_EXECUTION_CONTEXT,
                    OMITTED_BUDGET, OMITTED_COLD, OMITTED_NOT_PERMITTED,
                    OMITTED_NOT_PRESENT,
                    PACKAGE_NOT_READY, PROJECTED, REFUSED,
                    UNSUPPORTED_PROJECTION_PROFILE, ContextEconomy, Omission,
                    ProjectedPayload, ProjectionResult, Truncation)

READY = "READY"
NEITHER = "NEITHER"
FULL_BODY = "FULL_BODY"
FRAGMENT = "FRAGMENT"

# Material never injected automatically at any budget. It is selection
# mechanics and derived navigation rather than execution input, and
# `architecture.md` is on-demand only in M2 itself.
COLD_PACKAGE_KEYS = ("selection_trace", "omissions", "assumptions",
                     "navigation")
COLD_FOUNDATIONS = ("architecture.md",)

# Prose shorter than this is not worth truncating: the marker plus a stub
# costs nearly as much as the omission it replaces, and a two-line fragment of
# a constraint register is more misleading than its absence.
MIN_TRUNCATION_CHARS = 200

# No single optional section may take more than this share of the budget,
# whether it arrives whole or truncated. Without the cap, one large
# high-priority document (a constraint register, a long task body) expands to
# fill the budget and starves everything below it, so a strict priority order
# would produce a payload of one document and nothing else. Bounding each
# section is what makes the priority order express priority rather than mere
# arrival order. Leftover budget is redistributed by the backfill pass.
SECTION_MAX_SHARE = 0.4

TRUNCATION_MARKER = "\n...[TRUNCATED: kept {kept} of {original} chars]"

FAMILY_ORDER = ("Task", "Question", "Decision", "Investigation", "Evidence",
                "Handoff", "Report")

# The canonical payload section names, in priority order. This is the single
# source of the context-class taxonomy: anything that needs to reason about
# what a payload may contain (M3.4 skill governance) imports this rather than
# maintaining a second list. A test asserts it matches what `_candidates`
# actually produces.
PAYLOAD_SECTIONS = ("required", "constraints", "task_narrative",
                    "blocking_questions", "decisions", "continuation",
                    "investigations", "evidence", "open_questions", "reports",
                    "advisory_diagnostics", "charter", "current_state",
                    "code_context")

# Frontmatter carried per family: what a worker needs to act, not everything
# the record holds. Timestamps, creator identity, and reference lists are
# selection provenance rather than execution input and are left behind.
METADATA_FIELDS = {
    "Task": ("title", "objective", "status", "approval_required",
             "approval_status"),
    "Question": ("title", "question", "status", "impact"),
    "Decision": ("title", "question", "status", "approved_by", "approved_at"),
    "Investigation": ("title", "objective", "status", "conclusion_status"),
    "Evidence": ("title", "observation", "source_type", "status",
                 "reliability", "source"),
    "Handoff": ("title",),
    "Report": ("title",),
}


def _as_dict(package: Any) -> dict[str, Any]:
    """Accept the Context Package object or its already-serialized dict form."""
    if hasattr(package, "to_dict"):
        return package.to_dict()
    if isinstance(package, dict):
        return package
    raise TypeError("projection requires a Context Package or its dict form")


def _governed_view(pkg: dict[str, Any]) -> dict[str, Any]:
    """The package minus COLD material: what is eligible for projection."""
    view = {k: v for k, v in pkg.items() if k not in COLD_PACKAGE_KEYS}
    foundations = view.get("foundations")
    if isinstance(foundations, dict):
        view["foundations"] = {k: v for k, v in foundations.items()
                               if k not in COLD_FOUNDATIONS}
    return view


def _cold_present(pkg: dict[str, Any]) -> tuple[str, ...]:
    present = [key for key in COLD_PACKAGE_KEYS if pkg.get(key)]
    foundations = pkg.get("foundations") or {}
    present += [name for name in COLD_FOUNDATIONS if foundations.get(name)]
    return tuple(sorted(present))


def _truncate(text: str, limit: int | None) -> tuple[str, int | None]:
    """Bounded, marked truncation of narrative prose.

    Returns (text, kept_chars) where kept_chars is None when nothing was cut.
    The marker is always visible: a worker must be able to tell that it is
    reading part of a document rather than all of it.
    """
    if limit is None or len(text) <= limit:
        return text, None
    kept = max(0, limit)
    return (text[:kept] + TRUNCATION_MARKER.format(kept=kept,
                                                   original=len(text)), kept)


def _item(record: dict[str, Any], prose_limit: int | None,
          section: str, truncations: list[Truncation]) -> dict[str, Any]:
    """One selected record, reduced to identity, state, and permitted prose.

    Identity and provenance (`id`, `path`, `load_mode`) always survive, so the
    origin of any included content stays checkable. `load_mode` is copied, not
    chosen: projection can never load more of a record than M2 selected.
    """
    family = record.get("family", "")
    out: dict[str, Any] = {"id": record.get("id"), "path": record.get("path"),
                           "load_mode": record.get("load_mode")}
    if record.get("annotations"):
        # Annotations carry governance meaning (RECENT,
        # ACTIVE_DOES_NOT_MEAN_TRUE) and are short. They are never dropped.
        out["annotations"] = list(record["annotations"])
    if record.get("fragment"):
        out["fragment"] = record["fragment"]

    metadata = record.get("metadata") or {}
    for field_name in METADATA_FIELDS.get(family, ()):
        if field_name in metadata:
            out[field_name] = metadata[field_name]

    body = record.get("body")
    if record.get("load_mode") in (FULL_BODY, FRAGMENT) and isinstance(body, str) and body:
        text, kept = _truncate(body, prose_limit)
        out["body"] = text
        if kept is not None:
            truncations.append(Truncation(section=section,
                                          item_id=str(record.get("id")),
                                          field_name="body",
                                          original_chars=len(body),
                                          kept_chars=kept))
    return out


def _sorted_selected(pkg: dict[str, Any]) -> list[dict[str, Any]]:
    """Selected records in a stable order independent of caller ordering."""
    rank = {family: i for i, family in enumerate(FAMILY_ORDER)}
    selected = [s for s in (pkg.get("selected") or []) if isinstance(s, dict)]
    return sorted(selected, key=lambda s: (rank.get(s.get("family"), 99),
                                           str(s.get("id")),
                                           str(s.get("fragment") or "")))


def _required_section(pkg: dict[str, Any], intent: str) -> dict[str, Any]:
    """Non-droppable material. An executable payload cannot exist without it.

    Governance state, root Task identity and approval state, and every
    BLOCKING diagnostic. A payload that dropped any of this would let a worker
    act without knowing whether it is permitted to.
    """
    entry = pkg.get("entry") or {}
    compatibility = entry.get("compatibility") or {}
    root = pkg.get("root_task") or {}
    metadata = root.get("metadata") or {}

    # Absolute filesystem paths from `entry` are deliberately excluded: they
    # are machine-specific, they are not execution input, and they would leak
    # the operator's directory layout into a model payload.
    governance = {
        "package_status": pkg.get("package_status"),
        "task_actionability": pkg.get("task_actionability"),
        "entry_mode": entry.get("mode"),
        "ruleset_compatible": compatibility.get("compatible", False),
        "required_ruleset": compatibility.get("required_ruleset"),
        "approval": {
            "approval_required": metadata.get("approval_required"),
            "approval_status": metadata.get("approval_status"),
            "approved_scope_present": "approved_scope" in metadata,
        },
    }
    task = {"id": root.get("id"), "path": root.get("path"),
            "load_mode": root.get("load_mode"),
            "native_status": root.get("native_status")}
    for field_name in ("title", "objective", "status"):
        if field_name in metadata:
            task[field_name] = metadata[field_name]

    blocking = [
        {"category": d.get("category"), "code": d.get("code"),
         "blocking_dimension": d.get("blocking_dimension"),
         "semantic_owner": d.get("semantic_owner"), "target": d.get("target"),
         "reason": d.get("reason")}
        for d in (pkg.get("diagnostics") or [])
        if isinstance(d, dict) and d.get("blocking_dimension") != NEITHER
    ]

    required = {"intent": intent, "governance": governance, "task": task}
    if blocking:
        required["blocking_diagnostics"] = blocking
    return required


def _candidates(pkg: dict[str, Any]) -> list[tuple[str, Any, bool]]:
    """Optional sections in strict priority order: (name, builder, truncatable).

    THE ORDER IS THE POLICY. It is derived from M2 semantics and worker
    usefulness, never from filesystem order, dict ordering, or inferred
    relevance:

        1  constraints        hard limits on what may be done
        2  task_narrative     the plan and body of the work itself
        3  blocking_questions what currently blocks actionability
        4  decisions          governed choices already made
        5  continuation       the most recent Handoff: how to resume
        6  investigations     analysis in progress
        7  evidence           support for claims
        8  open_questions     non-blocking questions
        9  reports            what already happened
        10 advisory_diagnostics  non-blocking diagnostics, codes only
        11 charter            stable project purpose
        12 current_state      DERIVED and explicitly non-authoritative, so it
                              is the first thing to go

    Lower-priority sections are dropped first when the budget binds.
    """
    foundations = pkg.get("foundations") or {}
    selected = _sorted_selected(pkg)
    root_id = (pkg.get("root_task") or {}).get("id")

    def by_family(family: str, predicate=None) -> list[dict[str, Any]]:
        return [s for s in selected
                if s.get("family") == family and s.get("id") != root_id
                and (predicate is None or predicate(s))]

    def doc(name: str):
        def build(limit, truncations, section):
            text = foundations.get(name)
            if not isinstance(text, str) or not text:
                return None
            body, kept = _truncate(text, limit)
            if kept is not None:
                truncations.append(Truncation(section=section, item_id=name,
                                              field_name="text",
                                              original_chars=len(text),
                                              kept_chars=kept))
            return {"source": f".ai/{name}", "text": body}
        return build

    def records(family: str, predicate=None):
        def build(limit, truncations, section):
            items = by_family(family, predicate)
            if not items:
                return None
            return [_item(r, limit, section, truncations) for r in items]
        return build

    def task_narrative(limit, truncations, section):
        root = pkg.get("root_task") or {}
        metadata = root.get("metadata") or {}
        out: dict[str, Any] = {}
        if isinstance(metadata.get("plan"), str) and metadata["plan"]:
            text, kept = _truncate(metadata["plan"], limit)
            out["plan"] = text
            if kept is not None:
                truncations.append(Truncation(
                    section=section, item_id=str(root.get("id")),
                    field_name="plan", original_chars=len(metadata["plan"]),
                    kept_chars=kept))
        body = root.get("body")
        if root.get("load_mode") in (FULL_BODY, FRAGMENT) and isinstance(body, str) and body:
            text, kept = _truncate(body, limit)
            out["body"] = text
            if kept is not None:
                truncations.append(Truncation(
                    section=section, item_id=str(root.get("id")),
                    field_name="body", original_chars=len(body),
                    kept_chars=kept))
        return out or None

    def advisory(limit, truncations, section):
        # Codes and categories only. The category and code are never
        # shortened; the prose reason is simply not carried for a
        # non-blocking diagnostic, which omits detail without reinterpreting
        # anything.
        items = [{"category": d.get("category"), "code": d.get("code"),
                  "target": d.get("target")}
                 for d in (pkg.get("diagnostics") or [])
                 if isinstance(d, dict) and d.get("blocking_dimension") == NEITHER]
        return items or None

    def blocking_q(record):
        return (record.get("metadata") or {}).get("impact") == "BLOCKING"

    def open_q(record):
        return (record.get("metadata") or {}).get("impact") != "BLOCKING"

    def continuation(limit, truncations, section):
        handoffs = by_family("Handoff")
        recent = [h for h in handoffs if "RECENT" in (h.get("annotations") or [])]
        chosen = recent or handoffs
        if not chosen:
            return None
        return [_item(h, limit, section, truncations) for h in chosen]

    def code_context(limit, truncations, section):
        raw_code = pkg.get("code_context")
        if not raw_code:
            return None
        files = raw_code.get("files", []) if isinstance(raw_code, dict) else raw_code
        if not files or not isinstance(files, list):
            return None

        out_files = []
        for f in files:
            if not isinstance(f, dict):
                continue
            path = f.get("path", "")
            code = f.get("code", "")
            is_skel = f.get("is_skeleton", False)
            if limit is not None and len(code) > limit:
                pruned = generate_skeleton(code) if not is_skel else code
                text, kept = _truncate(pruned, limit)
                if kept is not None:
                    truncations.append(Truncation(
                        section=section, item_id=path,
                        field_name="code", original_chars=len(code),
                        kept_chars=kept))
                out_files.append({"path": path, "code": text, "is_skeleton": True})
            else:
                out_files.append({"path": path, "code": code, "is_skeleton": is_skel})
        return {"files": out_files} if out_files else None

    return [
        ("constraints", doc("constraints.md"), True),
        ("task_narrative", task_narrative, True),
        ("blocking_questions", records("Question", blocking_q), True),
        ("decisions", records("Decision"), True),
        ("continuation", continuation, True),
        ("investigations", records("Investigation"), True),
        ("evidence", records("Evidence"), True),
        ("open_questions", records("Question", open_q), True),
        ("reports", records("Report"), True),
        ("advisory_diagnostics", advisory, False),
        ("charter", doc("project.md"), True),
        ("current_state", doc("current-state.md"), True),
        ("code_context", code_context, True),
    ]


def _fits(chosen: dict[str, Any], name: str, value: Any,
          ceiling: int) -> bool:
    return len(compact({**chosen, name: value})) <= ceiling


def _largest_prose_limit(chosen, name, build, ceiling) -> int | None:
    """Largest prose allowance that still fits, by bounded binary search.

    Deterministic: a fixed number of iterations over a monotone predicate, so
    the same inputs always yield the same allowance.
    """
    low, high, best = 0, ceiling, None
    for _ in range(16):
        if low > high:
            break
        mid = (low + high) // 2
        value = build(mid, [], name)
        if value is not None and _fits(chosen, name, value, ceiling):
            best, low = mid, mid + 1
        else:
            high = mid - 1
    return best


def project(package: Any, intent: str, profile: str = "HOT",
            limit_tokens: int | None = None, estimator=None,
            allowed_sections: tuple[str, ...] | None = None
            ) -> ProjectionResult:
    """Project a governed Context Package into a budgeted worker payload.

    Returns a structured result for every expected governed state. Raises only
    on type misuse.

    `allowed_sections` restricts which optional sections MAY be emitted. It is
    applied BEFORE any section is built, so a forbidden class is never
    projected and then removed — stripping after the fact would leave the
    reported economy metrics describing a payload that no longer exists. This
    is the wiring point for M3.4 effective context classes (HB-M3-04); `None`
    means no caller restriction, which is the M3.3 behavior.

    The `required` section is governance metadata rather than project context —
    package status, task actionability, root Task identity, approval state, and
    blocking diagnostics — and is always emitted. A caller that wants to refuse
    an execution whose effective classes exclude `required` must do so before
    calling, because a payload without it would let a worker act without
    knowing whether it is permitted to.
    """
    pkg = _as_dict(package)
    budget = resolve_profile(profile, limit_tokens, estimator)

    est = budget.estimator if budget else resolve_profile("HOT").estimator
    governed_view = _governed_view(pkg)
    available = measure(pkg, est)
    governed = measure(governed_view, est)
    cold = _cold_present(pkg)

    def refuse(codes, payload=None, payload_size=None, tokens=0):
        size = payload_size or {"chars": 0, "estimated_tokens": 0}
        return ProjectionResult(
            outcome=REFUSED,
            profile=profile,
            budget_limit_tokens=budget.limit_tokens if budget else 0,
            estimated_payload_tokens=tokens,
            within_budget=False,
            economy=ContextEconomy(
                available_context=available,
                governed_context=governed,
                payload_size=size,
                context_avoided={
                    "chars": governed["chars"] - size["chars"],
                    "estimated_tokens": governed["estimated_tokens"]
                    - size["estimated_tokens"]}),
            payload=payload,
            reason_codes=tuple(codes),
            cold_context_available=cold,
            estimator=est.name)

    if budget is None:
        # COLD is not an executable budget, and an unknown profile is not
        # silently downgraded to a known one.
        return refuse([UNSUPPORTED_PROJECTION_PROFILE])

    if pkg.get("package_status") != READY:
        return refuse([PACKAGE_NOT_READY])

    if not pkg.get("root_task"):
        # A project-orientation package has no Task to execute against. That
        # is a legitimate governed state, not an error.
        return refuse([MISSING_REQUIRED_EXECUTION_CONTEXT])

    truncations: list[Truncation] = []
    omissions: list[Omission] = []
    limit_chars = budget.limit_chars

    required = _required_section(pkg, intent)
    chosen: dict[str, Any] = {"required": required}
    required_size = measure(chosen, est)
    if required_size["estimated_tokens"] > budget.limit_tokens:
        # Never silently truncate governance-critical material, and never
        # claim a fit the estimator does not support. An honest overflow is
        # the correct outcome.
        return refuse([BUDGET_OVERFLOW],
                      payload_size=required_size,
                      tokens=required_size["estimated_tokens"])

    candidates = _candidates(pkg)
    if allowed_sections is not None:
        permitted = set(allowed_sections)
        excluded = [name for name, _, _ in candidates if name not in permitted]
        candidates = [c for c in candidates if c[0] in permitted]
        for name in excluded:
            omissions.append(Omission(
                section=name, reason=OMITTED_NOT_PERMITTED, count=1,
                detail="not in the effective allowed context classes"))
    dropped: set[str] = set()

    # Pass 1 — strict priority order, each section bounded by its share.
    section_cap = int(limit_chars * SECTION_MAX_SHARE)
    for name, build, truncatable in candidates:
        value = build(None, [], name)
        if value is None:
            omissions.append(Omission(section=name,
                                      reason=OMITTED_NOT_PRESENT, count=0))
            continue
        used = len(compact(chosen))
        ceiling = min(limit_chars, used + section_cap)
        if _fits(chosen, name, value, ceiling):
            chosen[name] = build(None, truncations, name)
            continue
        if truncatable:
            allowance = _largest_prose_limit(chosen, name, build, ceiling)
            if allowance is not None and allowance >= MIN_TRUNCATION_CHARS:
                chosen[name] = build(allowance, truncations, name)
                continue
        dropped.add(name)

    # A section skipped above is never reconsidered: the loop keeps going
    # after a drop, so every later section is already evaluated against the
    # remaining budget, and admitting more material only ever reduces it. A
    # second "backfill" pass could therefore never admit anything the first
    # pass rejected.
    for name, _, _ in candidates:
        if name in dropped:
            omissions.append(Omission(
                section=name, reason=OMITTED_BUDGET, count=1,
                detail="did not fit the budget at this profile"))

    for name in cold:
        omissions.append(Omission(section=name, reason=OMITTED_COLD, count=1,
                                  detail="never injected automatically"))

    # Sections are emitted in canonical priority order regardless of which
    # pass admitted them, so the payload always reads in priority order.
    order = ["required"] + [name for name, _, _ in candidates]
    payload = ProjectedPayload(
        sections=tuple((name, chosen[name]) for name in order
                       if name in chosen))
    payload_size = measure(payload.to_dict(), est)
    return ProjectionResult(
        outcome=PROJECTED,
        profile=profile,
        budget_limit_tokens=budget.limit_tokens,
        estimated_payload_tokens=payload_size["estimated_tokens"],
        within_budget=payload_size["estimated_tokens"] <= budget.limit_tokens,
        economy=ContextEconomy(
            available_context=available,
            governed_context=governed,
            payload_size=payload_size,
            context_avoided={
                "chars": governed["chars"] - payload_size["chars"],
                "estimated_tokens": governed["estimated_tokens"]
                - payload_size["estimated_tokens"]}),
        payload=payload,
        truncations=tuple(truncations),
        omissions=tuple(omissions),
        cold_context_available=cold,
        estimator=est.name)
