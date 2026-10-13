---
name: brief
description: Prepare a service quote using two declared native tools.
---

Read these instructions before executing any capability.
Call calculate_total with the supplied unit_price and quantity.
Call lookup_runbook with the supplied service.
Return one JSON object with total from the first result and runbook from the
second result. Do not invent missing values or delegate to another agent.