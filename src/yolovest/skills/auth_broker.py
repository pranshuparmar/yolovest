"""Skill: auth-broker — Daily broker authentication.

Covers: FR-6.3
Trigger: CRON — daily at 9:00 AM IST (before market open)
Pipeline position: First skill of the day, everything depends on this.

Flow:
1. Send Telegram reminder to user with Kite login URL
2. Wait for user to paste request_token (via Telegram reply)
3. Exchange request_token → access_token via Kite API
4. Store access_token for the session
5. Verify connectivity: fetch account margins as health check
6. If auth fails, retry up to 3 times then alert via Telegram
"""

from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger


class AuthBrokerSkill(SkillBase):
    name = "auth-broker"
    description = "Daily Kite Connect re-authentication"
    trigger = SkillTrigger.CRON
    schedule = "0 9 * * 1-5"  # 9:00 AM IST, weekdays only

    def should_run(self) -> bool:
        # Run if we don't have a valid access_token for today
        return not self.ctx.broker.is_authenticated()

    async def execute(self, **kwargs: Any) -> SkillResult:
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
        access_token = await self.ctx.broker.authenticate(request_token)

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
