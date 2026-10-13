# Marketing playbook (TikTok slideshows + creator codes)

Written 2026-10-13. Not legal advice: have a lawyer review the creator agreement and the site's Privacy Policy / Terms before you scale.

## Ground rules (these protect you)
- **Never promise profit.** No "locks", "guaranteed", "free money". Say "tracked", "transparent", "+EV means long-run edge, not a sure win".
- **Show losses.** The site's pitch is that every settled pick is public. Slideshows that show a losing day build more trust than only wins.
- **Always include:** 21+ / "Bet responsibly" / "Not financial advice" / 1-800-GAMBLER on any slide that shows a pick.
- **Don't target minors**, don't use sportsbook logos as if endorsed, don't imply affiliation with any sportsbook or league.
- **TikTok restricts gambling promotion.** Keep it educational/analysis content, link only in your bio, avoid "bet now" calls to action and avoid showing deposit/bonus offers. Rules change; re-read TikTok's current gambling policy before posting.
- **Creators must disclose** the relationship in the video itself and caption (#ad / #affiliate / "I get paid if you sign up"). FTC rules apply to you and to them.

## What you can truthfully say right now
- Official picks are now **Pinnacle-referenced only** (a sharp, low-vig reference price). The earlier consensus-only picks were removed from the official record because they did not hold up.
- The record **restarted on the reset date** and the earlier results are still published under "All-time record".
- Every pick shows the book, the odds, the model's fair price and the EV at the time it was posted.
- Do NOT say the model "beats the market" until you have a few hundred settled picks with positive closing-line value. Early results are small-sample and noisy either way.

## Slideshow formats (6-8 slides each, native TikTok photo mode)
1. **"What +EV actually means"** - slide 1 hook ("Most bettors never check this number"), 2 implied vs fair probability, 3 worked example with real odds, 4 why one bet proves nothing, 5 what a 100-bet sample looks like, 6 CTA "I track mine publicly: link in bio".
2. **"Yesterday's picks, including the losses"** - screenshot of settled picks (W/L), cumulative units chart, one honest sentence on variance. Post daily; consistency beats polish.
3. **"Why books disagree"** - show one prop priced differently at three books and how a sharp reference (Pinnacle) is used to find the outlier.
4. **"How I'd size a bet"** - flat units vs Kelly, why 1u = 1% of bankroll, never chase.
5. **"Closing line value in 60 seconds"** - the one metric that tells you if a bettor is actually sharp.
6. **Weekly recap** - record since the reset, units, ROI, biggest miss and what you learned.

Hook patterns that work for betting-education content: a counter-intuitive claim ("A 60% win rate can still lose money"), a number ("I tracked 100 bets"), or a mistake ("The bet I should not have made").

## Posting plan (alt account first)
- Week 1: 1-2 posts/day, formats 1-3, link in bio with `?utm_source=tiktok&utm_campaign=alt`. Don't pay for anything yet.
- Track what drives signups (Creators tab + the UTM source), double down on the top 2 formats.
- Only after ~4 weeks of public record: approach creators.

## Creator program (attribution is built; payouts are manual until billing exists)
- Each creator gets a **code** (e.g. `ALEX10`) and a link `https://<your-site>/?ref=ALEX10`. The code is saved on the account at signup; the **Creators** tab in the operator dashboard shows signups, last-7-day signups, email-verified and marketing-opt-in counts per code.
- **No discount or payment is applied automatically**: there is no billing system. Until there is, pay a flat fee per verified signup or per month by hand, and say so in writing.
- When you add paid plans (e.g. Stripe), a revenue share (commonly 20-30% of the first N months) becomes a simple query on `customer_accounts.referral_code`.
- Put in a one-page written agreement: disclosure requirement, no profit guarantees, no targeting minors, no sportsbook endorsements, right to deactivate the code (button in the Creators tab), how and when you pay.

## Before you push traffic (checklist)
1. SendGrid sender-domain authentication (so verification/reset emails stop landing in spam) and `SITE_BASE_URL`, `SENDGRID_*`, `MLB_SUPPORT_EMAIL` set on the customer service.
2. Lawyer review of the Privacy Policy / Terms drafts and the marketing-consent text (you collect phone numbers).
3. Render Postgres backups on and a restore tested; SportsGameOdds key rotated; repo private.
4. Phone test: sign up, verify email, open EV Picks, log out/in.
5. Decide free vs paid: with no billing, everything is free, so a creator "profit share" has nothing to share yet.
