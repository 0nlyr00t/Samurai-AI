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
}


def profile_for(specialization: AgentSpecialization) -> SpecializationProfile:
    return PROFILES[specialization]
