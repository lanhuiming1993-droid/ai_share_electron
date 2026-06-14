---
name: a-share-growth-hunter
description: "A-share growth-stock analysis framework for Hermes + AlphaDesk. Use for A-share company, stock, growth, Davis double play, valuation, order, capacity, financial inflection, and 100-350 CNY bn market-cap opportunity analysis."
user-invocable: true
argument-hint: "[A-share company name, stock code, or growth-stock question]"
---

# A-Share Growth Hunter For Hermes

This skill is an analysis framework, not a data source and not a collector. Hermes must use AlphaDesk as the evidence base, then use external skills such as announcement search, research report search, finance query, market query, event query, business query, industry query, institutional research query, and A-stock selector for cross-validation.

## Boundary

- Do not call an LLM from AlphaDesk backend or collectors. Hermes is the only analyst.
- Do not invent market data, financial data, order size, capacity, customer names, institution count, or catalyst dates.
- Private sources from WeRSS, IMA, and ZSXQ are clues unless cross-validated by announcements, filings, financial reports, exchange Q&A, company disclosures, or credible research reports.
- If AlphaDesk evidence conflicts with external skill evidence, show the conflict, timestamp, source, and confidence instead of silently merging it.
- This framework must produce PDF-friendly structured HTML through the AlphaDesk report renderer.

## Trigger

Use this framework when the user asks about:

- A single A-share company or stock code.
- Growth-stock quality, Davis double play, valuation elasticity, inflection point, or opportunity/risk.
- Orders, customers, capacity ramp, gross margin, product cycle, industry position, institution consensus, or catalyst calendar.

Do not force this framework onto generic source aggregation reports, macro market questions, or broad industry/sector summaries unless the user explicitly asks for growth-stock screening or a company-level conclusion.

## Core Lens

Focus on the 100-200 CNY bn growth acceleration zone and the 200-350 CNY bn golden hitting zone:

- 50-100 CNY bn: transition zone. Survival, cash flow, order certainty, and ability to enter acceleration zone matter most.
- 100-200 CNY bn: growth acceleration zone. Initial financial inflection plus order/customer momentum can create high elasticity, but fraud and over-extrapolation risk are high.
- 200-350 CNY bn: golden hitting zone. Performance acceleration, narrative resonance, liquidity, and institution consensus often align best.
- 350-500 CNY bn: mid-cap leader zone. Upside elasticity declines; focus moves to durability and platform expansion.
- Above 500 CNY bn: large-cap pricing zone. Focus on durable profit scale and industry pricing power.

## Six-Dimension Framework

Score every company-level report with these six dimensions. Use clear labels such as positive, neutral, negative, or insufficient evidence.

1. Financial Inflection, 30 percent weight:
   Revenue/profit acceleration, gross margin inflection, operating cash flow, receivables, inventory, and asset quality. Use latest annual/quarterly disclosed data. If data is stale, label it as stale.

2. Industry Position, 25 percent weight:
   Domestic substitution rate, technical barrier, market-share rank, product uniqueness, customer adoption curve, and whether the segment is in the 10-30 percent penetration acceleration stage.

3. Customers And Orders, 20 percent weight:
   Named top customers, batch supply, framework agreements, repeat orders, contract liability/prepayment trend, customer concentration, and whether order evidence is one-off or repeatable.

4. Capacity And Delivery, 15 percent weight:
   Capacity ramp, utilization, yield, delivery cycle, capex discipline, and whether new capacity matches verified demand.

5. Shareholders And Institutions, 5 percent weight:
   Fund holding trend, shareholder count trend, insider increase/decrease, industrial capital, management background, and whether institutional coverage is improving or thin.

6. Catalyst Calendar, 5 percent weight:
   Product launch, customer certification, capacity release, earnings preview/report, policy event, industry conference, or other dated events within the next six months.

## Red Flags

Any triggered red flag must downgrade the conclusion:

- Receivables / net profit above 300 percent, or receivables / revenue above 50 percent when profit is negative.
- Non-recurring profit dominates reported profit.
- Operating cash flow remains negative while revenue growth is highlighted.
- Gross margin deteriorates for two consecutive quarters.
- Customer concentration above 70 percent without stable long-term contract evidence.
- Major shareholder or executive material reduction.
- Pledge ratio, goodwill, acquisition promises, or impairment risk materially affects valuation.
- The claim is based only on private clues with no public cross-validation.

## Required Output Sections

When this framework is active, the HTML report must include these sections in addition to normal AlphaDesk source cards:

- Market-cap zone and current positioning.
- Six-dimension scorecard with evidence source tags.
- Public disclosure versus private clue consistency check.
- Right-side confirmation signals, such as revenue threshold, gross margin recovery, named customer/order confirmation, capacity utilization, or institution consensus improvement.
- Invalidation signals, such as order delay, gross margin miss, cash-flow deterioration, dilution, major customer loss, or sector valuation compression.
- Conclusion grade: high-conviction, watchlist, cautious, or avoid. If evidence is insufficient, use watchlist or cautious instead of a forced bullish/bearish call.

## Evidence Labels

- Fact: directly supported by announcement, filing, financial report, market/finance skill output, or quoted AlphaDesk snapshot.
- Inference: reasoned synthesis across multiple evidence items.
- Unverified: private clue, stale data, single-source claim, or missing timestamp.

Never write investment advice as a command to buy or sell. Frame conclusions as research judgement, evidence confidence, and conditions to verify.
