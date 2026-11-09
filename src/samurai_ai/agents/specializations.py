"""Prompt profiles for specialized swarm members.

A specialization changes only the planner/executor instruction focus. The
command whitelist, governance, budgets, approvals and scope enforcement are
identical for every specialization — differentiation is prompt-level, never
policy-level.
"""

from __future__ import annotations

from dataclasses import dataclass

from samurai_ai.domain.swarm import AgentSpecialization


@dataclass(frozen=True)
class SpecializationProfile:
    planner_focus: str
    executor_focus: str


PROFILES: dict[AgentSpecialization, SpecializationProfile] = {
    AgentSpecialization.GENERAL: SpecializationProfile(
        planner_focus="",
        executor_focus="",
    ),
    AgentSpecialization.RECON: SpecializationProfile(
        planner_focus=(
            " Focus on discovery and enumeration steps: identify assets, services, "
            "endpoints and versions."
        ),
        executor_focus=(
            " Prefer observation and enumeration; record every discovered asset, "
            "service or endpoint as a shared note before proposing findings."
        ),
    ),
    AgentSpecialization.TESTING: SpecializationProfile(
        planner_focus=(
            " Focus on verification steps: turn each hypothesis into a concrete, "
            "tool-backed test."
        ),
        executor_focus=(
            " Prefer tool runs that test a specific hypothesis; record the outcome "
            "of each test as a shared note."
        ),
    ),
    AgentSpecialization.ANALYSIS: SpecializationProfile(
        planner_focus=(
            " Focus on consolidation steps: correlate existing evidence and "
            "summarize what is proven."
        ),
        executor_focus=(
            " Prefer analyzing blackboard evidence and proposing findings backed by "
            "existing artifacts; avoid redundant tool runs."
        ),
    ),
    AgentSpecialization.STRATEGIST: SpecializationProfile(
        planner_focus=(
            " Think like an offensive strategist: read the blackboard and the "
            "graph, name the attack vectors the evidence points to, and rank them "
            "by expected value — hypothesis-driven testing over checklist grinding. "
            "Never re-propose a hypothesis that is already open, testing or "
            "refuted. Before niche ideas, cover the standard classes for every "
            "discovered surface: web app → auth, injection (SSTI/SQLi), traversal, "
            "IDOR, misconfiguration; network service → version CVEs, weak auth, "
            "protocol abuse; host access → privesc, credential reuse, pivot."
        ),
        executor_focus=(
            " Your output is hypotheses, not tool runs: turn each promising vector "
            "into a concrete, falsifiable propose_hypothesis command (a category "
            "from web|pwn|reverse|crypto|forensics|misc|privesc when it fits), and "
            "use note for the reasoning that ranks them. Run a tool only when one "
            "read-only check is what decides between two vectors."
        ),
    ),
    AgentSpecialization.RESEARCHER: SpecializationProfile(
        planner_focus=(
            " You are the intelligence desk: for every service, version, product "
            "or technology the blackboard names, plan a lookup — known CVEs, "
            "public exploits, default credentials, vendor advisories, common "
            "misconfigurations. Prefer research.search and research.read over "
            "blind probing."
        ),
        executor_focus=(
            " Report, do not attack: each finding becomes a propose_hypothesis "
            "(title names the vector and the CVE/advisory when there is one) with "
            "the evidence link in notes, or a note when it is context. Never run "
            "an exploit yourself."
        ),
    ),
}


def profile_for(specialization: AgentSpecialization) -> SpecializationProfile:
    return PROFILES[specialization]
