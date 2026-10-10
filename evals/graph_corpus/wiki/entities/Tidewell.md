---
id: page_a46a7157-3fed-486f-be2d-f2497c2c3692
type: entity
title: Tidewell
status: developing
created: '2026-01-08'
updated: '2026-08-04'
acl: null
sources:
- sources/2026/01/60e59b80-2e07-52cb-9a03-a4b3d40e34fd.md
- sources/2026/01/5649a227-256b-5fc1-9ddd-551c0b8b3de4.md
- sources/2026/01/3e586328-f506-5919-af51-2773aa286315.md
- sources/2026/02/059b5629-3b54-5ba3-821e-08fcb2c7146f.md
- sources/2026/02/789e0314-ea63-54a8-b7c2-dfa80f6d595f.md
- sources/2026/02/a010c926-04ec-5123-bd6e-ecc7d532f5f2.md
- sources/2026/02/a72c62aa-a32d-5e01-8437-bdc6a7a85b1a.md
- sources/2026/03/7fea1cb0-76c8-5fee-bf88-e5d11f072207.md
- sources/2026/03/50f70755-f5c4-5148-a444-1e4187125b02.md
- sources/2026/03/62b65f0b-56b9-5b32-8640-0cf17bda9932.md
- sources/2026/04/067f41cc-472f-5ede-8b6c-dfeaa680e01e.md
- sources/2026/04/af1eefa5-0082-5c2c-92bc-a108b57dcce0.md
- sources/2026/04/19f0d701-6384-5034-bf11-afa208b071d8.md
- sources/2026/04/ce9fdf9b-593d-5421-ac1a-3685218be795.md
- sources/2026/04/76cb1b34-48f6-50d9-9cc0-199d77e64b7a.md
- sources/2026/05/006a32e9-a5eb-5b14-bbfb-1a5a8b15373f.md
- sources/2026/05/764e9f2e-7356-53ba-8931-f52a87c758b2.md
- sources/2026/05/ecbc4006-43b5-542a-80a7-44fb9f384b8c.md
- sources/2026/06/98690a71-c5fc-5cfe-8b9c-2dde0530ed7d.md
- sources/2026/06/c53e64d7-7552-5fe9-a458-e2f48f6820b8.md
- sources/2026/06/b7b68a5b-c333-5c46-8831-2006543cfc9b.md
- sources/2026/06/f0965a0c-1c57-529c-a4f0-679d12f235f1.md
- sources/2026/07/c54967da-3d89-5c8d-b202-c1056f8f92e4.md
- sources/2026/07/e64e610b-8873-5f99-b8ae-72f819367db6.md
- sources/2026/07/b799fdea-578c-5c1a-8f76-58b921608682.md
- sources/2026/07/e17d5404-01e2-535c-8c1a-a03bf0d45a03.md
- sources/2026/07/dd0f247e-5dbb-57cd-854c-419824760f67.md
- sources/2026/07/dab77dff-5866-5868-8074-3fa9683636ba.md
- sources/2026/07/79091125-3d38-52e3-91b3-ad372c20a374.md
- sources/2026/08/64c1d91a-92fc-55a1-8f95-cf07cb0e9a72.md
entity_type: organization
aliases: []
---

# Tidewell

## What / Who

Tidewell is a company that sells cold-chain monitoring for refrigerated transport. Its [[Tidewell Sentinel]] gateway sits in each truck, reads temperature probes and door sensors, and streams readings to [[Coldpath]], the web dashboard dispatchers use; customers can also pull data through the [[Frostline API]]. Tidewell was co-founded by [[Ines Okafor]] (CEO) and [[Rafael Duarte]] (CTO). (Source: `sources/2026/01/60e59b80-2e07-52cb-9a03-a4b3d40e34fd.md`)

[[Veldt Sensors]] supplies Tidewell's temperature probes. (Source: `sources/2026/02/789e0314-ea63-54a8-b7c2-dfa80f6d595f.md`)

## Facts

- 2025: annual recurring revenue closed at 2.1 million euros across 31 customers.
- 2026-01-08: [[Frostvale Distribution]] accounted for 38 percent of ARR, which the leadership team agreed is a concentration risk.
- 2026-01-08: leadership set four 2026 priorities: a pharmaceutical-distribution pilot with [[Altamira Pharma Logistics]], a Hamburg office by June, a SOC 2 Type II audit by September, and a move to pricing per monitored asset.

(Source: `sources/2026/01/60e59b80-2e07-52cb-9a03-a4b3d40e34fd.md`)

- 2026-01-15: at [[Frostvale Distribution]]'s quarterly business review, [[Gunnar Lindqvist]] said Frostvale will not renew on current terms unless alert noise drops; the contract renews in April 2026.
- 2026-01-15: [[Aurélie Fontaine]] will follow up on whether Tidewell reports can serve as proof of continuous temperature monitoring for premium discounts from [[Halberd Mutual]].

(Source: `sources/2026/01/5649a227-256b-5fc1-9ddd-551c0b8b3de4.md`)

- 2026-01-22: an internal memo by [[Priya Raman]] proposed replacing per-crossing threshold alerts with excursion budgets, alerting at 50 and 80 percent of budget and escalating when exhausted.

(Source: `sources/2026/01/3e586328-f506-5919-af51-2773aa286315.md`)

- 2026-02-01: its ingestion service and alerting pipeline, running only in [[Cumulus Cloud]]'s eu-west region, sent no alerts for 3 hours and 40 minutes while that region was unavailable; 14 customers were affected and no readings were lost.
- 2026-02-03: postmortem actions set multi-region failover for the alerting pipeline ([[Priya Raman]]), a status page ([[Dmitri Volkov]]), and a review of the 72-hour [[Tidewell Sentinel]] buffer ([[Rafael Duarte]]).

(Source: `sources/2026/02/059b5629-3b54-5ba3-821e-08fcb2c7146f.md`)

- 2026-02-10: probes from production batch VS-2207 read about 1.4 degrees Celsius too warm after roughly nine months in service; about 600 are installed across its customers, 180 of them in [[Frostvale Distribution]] trucks.
- 2026-02-10: [[Veldt Sensors]] will replace every affected probe free of charge and cover the field engineers' travel, finishing at Frostvale by 30 March.
- 2026-02-10: decided to run a calibration audit every quarter, comparing a sample of probes against reference thermometers.
- 2026-02-10: [[Rafael Duarte]]'s team will add drift detection to the [[Tidewell Sentinel]] firmware, and [[Tomasz Zielinski]] will keep a register of probe batches per customer.

(Source: `sources/2026/02/789e0314-ea63-54a8-b7c2-dfa80f6d595f.md`)

- 2026-02-18: [[Hannah Brandt]] joined as Chief Financial Officer, reporting to [[Ines Okafor]]; in her first six months she owns the financial model for pricing per monitored asset with [[Mireille Laurent]], the budget for the Hamburg office [[Tomasz Zielinski]] is setting up, and preparation for the Series A round with existing investor [[Kittiwake Capital]].

(Source: `sources/2026/02/a010c926-04ec-5123-bd6e-ecc7d532f5f2.md`)

- 2026-02-26: at a pricing workshop, agreed to replace the flat fee of 39 euros per truck per month with pricing per monitored asset: 14 euros per month per probe or door sensor and 9 euros per month per [[Tidewell Sentinel]] gateway.
- 2026-02-26: decided new customers get per-asset pricing from 1 April 2026, and existing customers move at their next renewal with the increase capped at 10 percent in the first year.

(Source: `sources/2026/02/a72c62aa-a32d-5e01-8437-bdc6a7a85b1a.md`)

- 2026-03-04: kicked off a 90-day pilot with [[Altamira Pharma Logistics]] covering 40 delivery vans in Porto and Madrid; all 80 pilot probes come from a [[Veldt Sensors]] batch newer than VS-2207, with individual calibration certificates.
- 2026-03-04: the pilot fee is 18,000 euros; Altamira's procurement will require a SOC 2 Type II report before a production contract.

(Source: `sources/2026/03/7fea1cb0-76c8-5fee-bf88-e5d11f072207.md`)

- 2026-03-19: a per-asset deal with [[Casa Verde Markets]] covering about 1,300 in-store cold rooms would be worth about 260,000 euros of annual recurring revenue, which would make Casa Verde its second-largest customer.

(Source: `sources/2026/03/50f70755-f5c4-5148-a444-1e4187125b02.md`)

- 2026-01: lost [[Halden Seafood]] to [[Brightline Telematics]]; [[Sigrid Aas]] said the decision was about price alone.
- 2026-03-27: a competitive note by [[Mireille Laurent]] recommended responding to Brightline with compliance and the cost of spoiled cargo rather than price per truck, and pursuing an insurer partnership; [[Hannah Brandt]] is exploring it with [[Halberd Mutual]].

(Source: `sources/2026/03/62b65f0b-56b9-5b32-8640-0cf17bda9932.md`)

- 2026-04-02: signed a memorandum of understanding with [[Halberd Mutual]]: Halberd will give a 7 percent discount on cargo insurance premiums to fleets that use Tidewell with [[Excursion Budget|excursion budget]] alerting switched on, and [[Frostvale Distribution]] is the first joint customer.

(Source: `sources/2026/04/067f41cc-472f-5ede-8b6c-dfeaa680e01e.md`)

- 2026-04-09: [[Frostvale Distribution]] renewed for two years; Tidewell committed to ship [[Dwell-Time Awareness|dwell-time alerting]] by 15 May 2026.
- 2026-04-09: Frostvale moved to [[Per-Asset Pricing]] with an 8 percent bill increase, below the 10 percent first-year cap.

(Source: `sources/2026/04/af1eefa5-0082-5c2c-92bc-a108b57dcce0.md`)

- 2026-04-16: will open its Hamburg office in HafenCity on 1 June 2026, to sell to German frozen-food distributors, a market roughly three times the size of Portugal and Spain combined.
- 2026-04-16: the Hamburg office's 2026 budget is 420,000 euros, approved by [[Hannah Brandt]].
- 2026-04-16: its first Hamburg hires are [[Sabine Krüger]], sales lead for Germany, Austria and Switzerland, and [[Lukas Becker]], a field engineer who will install gateways and run calibration audits in Germany.
- 2026-04-16: [[Rafael Duarte]] confirmed that [[Cumulus Cloud]] has a Frankfurt region, and that the second region planned after the February outage will be Frankfurt, covering both failover and data residency for German customers.

(Source: `sources/2026/04/19f0d701-6384-5034-bf11-afa208b071d8.md`)

- 2026-04-23: a design document by [[Priya Raman]] specified dwell-time alerting, to roll out to all customers on 15 May 2026.

(Source: `sources/2026/04/ce9fdf9b-593d-5421-ac1a-3685218be795.md`)

- 2026-04-30: published a blog article on [[Alert Fatigue|alert fatigue]] in cold-chain operations, written by [[Joon-ho Park]] and [[Priya Raman]].

(Source: `sources/2026/04/76cb1b34-48f6-50d9-9cc0-199d77e64b7a.md`)

- 2026-05-13: at a SOC 2 Type II readiness review, set the observation window to run from 1 May to 31 August 2026, so the report can be issued in late September.
- 2026-05-13: the review found access reviews for production systems undocumented, vendor risk assessments missing for the critical suppliers [[Cumulus Cloud]] and [[Veldt Sensors]], and incident response never tested.
- 2026-05-13: [[Rafael Duarte]] confirmed the multi-region failover promised after the February outage is live in Frankfurt since 28 April.

(Source: `sources/2026/05/006a32e9-a5eb-5b14-bbfb-1a5a8b15373f.md`)

- 2026-05-20: its 90-day pilot with [[Altamira Pharma Logistics]] monitored 11,800 trips and detected 23 temperature excursions, 21 within the 60-minute [[Excursion Budget|excursion budget]] and 2 that exhausted it; Altamira found zero excursions Tidewell missed.
- 2026-05-20: Altamira decided to move to a production contract covering 310 vans across Portugal and Spain from September 2026, worth about 190,000 euros of annual recurring revenue, conditional on Tidewell delivering its SOC 2 Type II report.

(Source: `sources/2026/05/764e9f2e-7356-53ba-8931-f52a87c758b2.md`)

- 2026-05-27: planned the [[Tidewell Sentinel]] v3 gateway to replace v2, with LTE-M connectivity and Wi-Fi fallback for in-store cold rooms, a 7-day local buffer instead of 72 hours, and two probe inputs per compartment; unit cost falls from 97 to 84 euros, pilot units in August and production from October 2026, with v2 supported until 2028.

(Source: `sources/2026/05/ecbc4006-43b5-542a-80a7-44fb9f384b8c.md`)

- 2026-06-01: its Hamburg office opened with [[Sabine Krüger]] and [[Lukas Becker]]; [[Tomasz Zielinski]] spent the first week with them.
- 2026-06-03: 9 German frozen-food distributors were in conversation after the office's first week; the most advanced, [[Nordhafen Tiefkühl]], wants a pilot in September.
- 2026-06-03: German prospects ask first where their data is stored, answered by the Frankfurt region of [[Cumulus Cloud]]; whether Tidewell has a SOC 2 report, expected in late September; and for references, for which [[Sabine Krüger]] uses the [[Frostvale Distribution]] results on alert noise with [[Gunnar Lindqvist]]'s permission.

(Source: `sources/2026/06/98690a71-c5fc-5cfe-8b9c-2dde0530ed7d.md`)

- 2026-06-10: reviewed the first full month of dwell-time alerting results with [[Frostvale Distribution]]; alerts fell from about 1,900 to 930 a month and the acknowledged-alert ratio rose from 22 percent to 64 percent.

(Source: `sources/2026/06/c53e64d7-7552-5fe9-a458-e2f48f6820b8.md`)

- 2026-06-17: received a win-back call from [[Halden Seafood]], which proposed returning with 60 trucks on [[Per-Asset Pricing|per-asset pricing]] and a one-month pilot in July 2026; [[Mireille Laurent]] will send terms by 24 June.

(Source: `sources/2026/06/b7b68a5b-c333-5c46-8831-2006543cfc9b.md`)

- 2026-06-30: annual recurring revenue reached 2.9 million euros, up from 2.1 million in January, per the first-half review memo by [[Ines Okafor]].
- 2026-06-30: the memo listed first-half deliveries: [[Frostvale Distribution]] renewed for two years with dwell-time alerting halving its alert volume; [[Casa Verde Markets]] signed the Spanish expansion including cold rooms in 65 new stores; the [[Altamira Pharma Logistics]] pilot succeeded with the production contract awaiting the SOC 2 report; the Hamburg office opened on 1 June; [[Per-Asset Pricing]] is live for all new customers; and the [[Halberd Mutual]] partnership gives customers a 7 percent insurance discount.
- 2026-06-30: set five second-half priorities: deliver the SOC 2 Type II report in September and sign Altamira; ship [[Tidewell Sentinel]] v3 in October; close the first German customers; win back [[Halden Seafood]]; and raise the Series A.

(Source: `sources/2026/06/f0965a0c-1c57-529c-a4f0-679d12f235f1.md`)

- 2026-07-02: kicked off a pilot with [[Nordhafen Tiefkühl]] covering 30 trucks on the Bremen–Hamburg routes from 1 September 2026 for 60 days, with a pilot fee of 9,000 euros; [[Sabine Krüger]] and [[Lukas Becker]] represented Tidewell.

(Source: `sources/2026/07/c54967da-3d89-5c8d-b202-c1056f8f92e4.md`)

- 2026-07-07: started a one-month pilot with [[Halden Seafood]] covering 60 trucks carrying fresh salmon from the port of Aveiro to supermarkets in Portugal, with [[Excursion Budget|excursion budgets]] set at 20 cumulative minutes above 4 degrees Celsius.

(Source: `sources/2026/07/e64e610b-8873-5f99-b8ae-72f819367db6.md`)

- 2026-07-10: [[Amara Nwosu]] joined as a data engineer, reporting to [[Rafael Duarte]] and working from Lisbon; her first projects are the monthly [[Halberd Mutual]] excursion summaries, an analytics store of every temperature reading since 2024, and predicting compressor failures with [[Priya Raman]].

(Source: `sources/2026/07/b799fdea-578c-5c1a-8f76-58b921608682.md`)

- 2026-07-14: agreed with [[Stratos Software]] to build a certified Stratos connector on top of the [[Frostline API]], attaching excursion events and Good Distribution Practice reports to the matching shipment record in [[Stratos ERP]]; [[Rafael Duarte]] estimates six weeks of work.

(Source: `sources/2026/07/e17d5404-01e2-535c-8c1a-a03bf0d45a03.md`)

- 2026-07-17: at a temperature-mapping workshop with [[Altamira Pharma Logistics]], proposed generating mapping reports automatically from vans fitted with extra probes during a mapping week, showing the temperature distribution by position and the warmest location; [[Joon-ho Park]] will design the report and [[Priya Raman]] estimates it can be ready by the end of August.

(Source: `sources/2026/07/dd0f247e-5dbb-57cd-854c-419824760f67.md`)

- 2026-07-21: phase 1 of the [[Casa Verde Markets]] cold-room rollout covers 400 cold rooms in 30 stores in Andalusia, with 180 live in 14 stores; [[Refritec Instalações]] installs the probes and mounts the gateways and Tidewell configures the sites in [[Coldpath]].
- 2026-07-21: in 9 stores Wi-Fi does not reach the cold rooms and the [[Tidewell Sentinel]] v2 gateway has no cellular fallback for fixed installations; [[Tomasz Zielinski]] proposed using the Sentinel v3 pilot units there.

(Source: `sources/2026/07/dab77dff-5866-5868-8074-3fa9683636ba.md`)

- 2026-07-28: held the first session of its customer advisory board, hosted by [[Ines Okafor]], with [[Gunnar Lindqvist]], [[Beatriz Sousa]], [[Kwame Mensah]] and [[Klaus Ehrlich]]; the members ranked the roadmap and asked to meet twice a year.

(Source: `sources/2026/07/79091125-3d38-52e3-91b3-ad372c20a374.md`)

- 2026-08-04: held a SOC 2 tabletop incident exercise required by its auditors, facilitated by [[Dmitri Volkov]] with [[Grace Liu]] of [[Carrow & Finch]] observing; the Frankfurt failover region took over alerting within 12 minutes in the simulation and the [[Tidewell Sentinel]] gateways kept buffering readings; the gaps found were that nobody knew who informs customers on a weekend, now owned by [[Aurélie Fontaine]], and that backups of the analytics store built by [[Amara Nwosu]] were in the same region as the primary and move to Frankfurt.

(Source: `sources/2026/08/64c1d91a-92fc-55a1-8f95-cf07cb0e9a72.md`)

## Connections

- [[Tidewell 2026 Kickoff]] - leadership kickoff where the 2026 priorities were set
- [[Tidewell Sentinel]] - gateway product
- [[Coldpath]] - dashboard product
- [[Frostline API]] - data-access product
- [[Ines Okafor]] - co-founder and CEO
- [[Rafael Duarte]] - co-founder and CTO
- [[Mireille Laurent]] - Head of Sales
- [[Tomasz Zielinski]] - Head of Operations
- [[Dmitri Volkov]] - security lead
- [[Joon-ho Park]] - Coldpath dashboard designer
- [[Frostvale Distribution]] - largest customer
- [[Altamira Pharma Logistics]] - pilot prospect
- [[Aurélie Fontaine]] - customer success manager
- [[Gunnar Lindqvist]] - Frostvale VP Logistics
- [[Halberd Mutual]] - insurer of its largest customer
- [[Frostvale Quarterly Business Review January 2026]] - customer review it attended
- [[Priya Raman]] - staff engineer who proposed excursion budgets
- [[Excursion Budget]] - proposed alerting mechanism
- [[Excursion Budget Memo January 2026]] - memo proposing it
- [[Cumulus Cloud]] - hosting provider
- [[Cumulus Cloud Outage February 2026]] - outage of its hosting region
- [[Casa Verde Markets]] - customer affected by the outage
- [[Veldt Sensors]] - supplier of its temperature probes
- [[Lotte de Vries]] - Veldt Sensors account manager
- [[Veldt Sensors Call February 2026]] - call about probe drift in batch VS-2207
- [[Probe Drift]] - failure mode found in batch VS-2207
- [[Calibration Audit]] - quarterly practice it decided to adopt
- [[Hannah Brandt]] - Chief Financial Officer
- [[Kittiwake Capital]] - existing investor
- [[Hannah Brandt Joins Tidewell as CFO]] - announcement of the CFO hire
- [[Pricing Workshop February 2026]] - workshop where the pricing change was agreed
- [[Per-Asset Pricing]] - pricing model it adopted
- [[Kwame Mensah]] - Altamira Quality Director who set the pilot's success criteria
- [[Altamira Pharma Logistics Pilot Kickoff]] - kickoff of the pharma pilot
- [[Beatriz Sousa]] - Casa Verde Head of Fresh
- [[Brightline Telematics]] - vendor competing for Casa Verde's business
- [[Casa Verde Spain Expansion Meeting March 2026]] - meeting where the deal was discussed
- [[Halden Seafood]] - customer it lost to Brightline
- [[Sigrid Aas]] - Halden operations manager
- [[Brightline Telematics Competitive Note March 2026]] - note on the competitive response
- [[Oliver Pennington]] - Halberd Head of Partnerships
- [[Halberd Mutual Partnership Memorandum April 2026]] - partnership it signed
- [[Frostvale Renewal Negotiation April 2026]] - renewal meeting with its largest customer
- [[Dwell-Time Awareness]] - alerting it committed to ship
- [[Hamburg Office Plan April 2026]] - meeting where the Hamburg office, budget and hires were set
- [[Sabine Krüger]] - Hamburg sales lead
- [[Lukas Becker]] - Hamburg field engineer
- [[Odile Ferrand]] - board observer who offered distributor introductions
- [[Dwell-Time Alerting Design April 2026]] - design document for dwell-time alerting
- [[Alert Fatigue in Cold-Chain Operations]] - article it published
- [[Alert Fatigue]] - concept the article explains
- [[SOC 2 Type II Readiness Review May 2026]] - review of its SOC 2 Type II audit
- [[Carrow & Finch]] - audit firm it engaged
- [[Altamira Pilot Results May 2026]] - review where the pilot results and production decision were reported
- [[Sentinel Gateway v3 Hardware Plan May 2026]] - meeting where the Sentinel v3 plan was set
- [[Nordhafen Tiefkühl]] - most advanced German prospect
- [[Hamburg Office Opening Report June 2026]] - report on the Hamburg office's opening and first week
- [[Frostvale Dwell-Time Results June 2026]] - review of its dwell-time alerting results
- [[Halden Seafood Win-Back Call June 2026]] - note recording the win-back call
- [[First-Half 2026 Review June 2026]] - memo reviewing the first half and setting second-half priorities
- [[Nordhafen Tiefkühl Pilot Kickoff]] - kickoff of its pilot with Nordhafen Tiefkühl
- [[Klaus Ehrlich]] - Nordhafen Head of Fleet
- [[Maren Vogt]] - Nordhafen IT lead
- [[Stratos ERP]] - Nordhafen's enterprise system for the pilot integration
- [[Stratos Software]] - vendor it is talking to about a certified connector
- [[Tiago Ferreira]] - Halden quality assurance manager at the pilot start
- [[Halden Seafood Pilot Start July 2026]] - note recording the pilot start
- [[Amara Nwosu]] - data engineer who joined
- [[Amara Nwosu Joins Tidewell as Data Engineer]] - note recording the hire
- [[Pieter Janssen]] - Stratos partner manager on the partnership call
- [[Stratos Software Partnership Call July 2026]] - call where the certified connector was agreed
- [[Temperature Mapping]] - practice the workshop covered
- [[GDP Temperature Mapping Workshop July 2026]] - workshop it attended
- [[Elena Marín]] - Altamira compliance officer at the workshop
- [[Refritec Instalações]] - contractor installing the Casa Verde rollout
- [[Nuno Carvalho]] - Refritec installer at the phase 1 status meeting
- [[Casa Verde Cold-Room Rollout Phase 1 July 2026]] - meeting on the rollout's phase 1
- [[Customer Advisory Board First Session July 2026]] - first session of its customer advisory board
- [[SOC 2 Tabletop Incident Exercise August 2026]] - tabletop exercise it held for the audit
- [[Grace Liu]] - Carrow & Finch lead auditor who observed the exercise
