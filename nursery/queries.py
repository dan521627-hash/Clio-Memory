"""Side-effect-free internal nursery status query."""

from __future__ import annotations

from .body_state import BodyTimeRule, effective_body_view
from .capability_rules import CapabilityRules, ability_stage_ceiling
from .models import ActorContext
from .privacy import public_status_view
from .store import NurseryStore


class NurseryQueryService:
    def __init__(self, store: NurseryStore, *, body_time_rule: BodyTimeRule | None = None,
                 capability_rules: CapabilityRules | None = None) -> None:
        self.store = store
        self.body_time_rule = body_time_rule or BodyTimeRule()
        self.capability_rules = capability_rules

    def get_status(
        self,
        *,
        account_id: str,
        child_id: str,
        actor: ActorContext | None = None,
    ) -> dict:
        """Return a state-specific whitelist without creating any operation."""

        if actor is not None:
            if actor.account_id != account_id:
                from .models import NurseryError

                raise NurseryError(
                    "ACTOR_SCOPE_MISMATCH", "actor cannot query another account"
                )
            self.store.assert_actor_registered(actor)
        row = self.store.get_state(account_id, child_id)
        candidate = {
            "module": "nursery",
            "module_state": row["module_state"],
            "state_version": int(row["state_version"]),
            "child_id": row["child_id"],
            "stage_id": row["stage_id"],
            "active_pause_count": int(row["active_pause_count"]),
            "recycle_deadline": row["recycle_deadline"],
            "restore_state": row["pre_delete_state"],
        }
        result = public_status_view(candidate)
        if result["module_state"] in {"active", "paused"}:
            runtime = self.store.get_child_runtime_state(child_id)
            body = effective_body_view(
                runtime,
                self.store.get_body_clock(child_id),
                evaluated_at=self.store.clock(),
                rule=self.body_time_rule,
                active=result["module_state"] == "active",
            )
            result["child_state"] = body["effective_runtime"]
        return result

    def get_child_status(
        self,
        *,
        actor: ActorContext,
        child_id: str,
        detail: str = "summary",
        since: str | None = None,
        query: str = "",
        cursor: str | None = None,
    ) -> dict:
        """Return the shared phone/MCP detail view, always without side effects."""

        result = self.store.get_child_feature_view(
            actor=actor,
            child_id=child_id,
            detail=detail,
            query=query,
            cursor=cursor,
        )
        if result.get("module_state") in {"active", "paused"}:
            runtime = self.store.get_child_runtime_state(child_id)
            body = effective_body_view(
                runtime,
                self.store.get_body_clock(child_id),
                evaluated_at=self.store.clock(),
                rule=self.body_time_rule,
                active=result["module_state"] == "active",
            )
            result["current_state"]["body_needs"] = body["body_needs"]
            result["current_state"]["effective_needs"] = body["effective_needs"]
            result["current_state"]["emotion"] = body["effective_runtime"].get("emotion", {})
            result["time_context"] = body["time_context"]
        if "growth_view" in result:
            growth = result["growth_view"]
            growth["abilities"] = []
            growth["rules_available"] = self.capability_rules is not None
            if self.capability_rules is not None:
                for ability in self.capability_rules.abilities:
                    ceiling = ability_stage_ceiling(self.capability_rules, growth["stage"], ability)
                    observations = [
                        item for item in growth["observations"]
                        if ability["id"] in item["ability_ids"]
                    ]
                    # A stage ceiling and recorded observations are not proof of mastery.
                    growth["abilities"].append({
                        "id": ability["id"], "label": ability.get("label", ability["id"]),
                        "dimension": ability["dimension"],
                        "can_record": ceiling != "blocked",
                        "stage_ceiling": ceiling,
                        "stage_status": {"blocked":"当前阶段未开放", "emerging":"可以观察初步尝试",
                                         "assisted":"可在养育者帮助下尝试", "independent":"阶段允许逐渐独立尝试，不代表已经掌握"}[ceiling],
                        "observation_count": len(observations),
                        "observed_sessions": len({item["session_id"] for item in observations}),
                        "latest_observation": observations[0] if observations else None,
                    })
                growth["rules_version"] = self.capability_rules.schema_version
        if since:
            result["since"] = str(since)
        return result
