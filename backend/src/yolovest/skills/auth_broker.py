"""Skill: auth-broker — Daily broker authentication.

Trigger: CRON — daily at 8:30 AM IST (before market open)
Pipeline position: First skill of the day, everything depends on this.

Flow:
1. Send Telegram reminder to user with Kite login URL
2. Wait for user to paste request_token (via Telegram reply)
3. Exchange request_token → access_token via Kite API
4. Store access_token for the session
5. Verify connectivity: fetch account margins as health check
6. If auth fails, retry up to 3 times then alert via Telegram
"""

import logging
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class AuthBrokerSkill(SkillBase):
    name = "auth-broker"
    description = "Daily Kite Connect re-authentication"
    trigger = SkillTrigger.CRON
    schedule = None  # set from config in __init__

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.schedule = self.compute_schedule()

    def compute_schedule(self) -> str | None:
        return self.ctx.config.heartbeat.auth_broker_cron

    def should_run(self) -> bool:
        # Always run on schedule — the execute() method handles
        # the actual auth check. is_authenticated() is async and
        # cannot be called from sync should_run().
        return True

    async def execute(self, **kwargs: Any) -> SkillResult:
        # Check if already authenticated — skip if session is still valid
        try:
            if await self.ctx.broker.is_authenticated():
                logger.info("auth-broker: already authenticated, skipping re-auth")
                return SkillResult(
                    success=True,
                    skill_name=self.name,
                    data={"authenticated": True, "skipped": True,
                          "reason": "session_still_valid"},
                )
        except Exception as e:
            logger.info("auth-broker: auth check failed (%s), proceeding with re-auth", e)

        # Step 1: Send Telegram reminder with login URL
        login_url = self.ctx.broker.get_login_url()
        await self.ctx.notify.send(
            f"🔐 Daily Kite login required.\n{login_url}\n"
            "Reply with the request_token from the redirect URL."
        )

        # Step 2: Wait for request_token (via Telegram callback or manual input)
        request_token = kwargs.get("request_token")
        if not request_token:
            return SkillResult(
                success=False,
                skill_name=self.name,
                error="Awaiting request_token from user",
                data={"status": "waiting_for_token", "login_url": login_url},
            )

        # Step 3: Exchange for access_token
        await self.ctx.broker.authenticate(request_token)

        # Step 4: Verify connectivity
        margins = await self.ctx.broker.get_margins()

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "authenticated": True,
                "available_cash": margins.get("available_cash"),
            },
        )
