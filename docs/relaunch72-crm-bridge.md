# Relaunch72 CRM read bridge

The founder-only `/crm` page reads the latest Property Predator CRM leads from
`https://hq.propertypredator.com/api/group-hq/crm/leads?brand=pp`. It never sends
messages or writes product data. Group HQ keeps its own people, mentees, grants,
growth metrics and Hello New Home franchise pipeline.

Set `R72_CRM_TOKEN` on the Group HQ web service only. Generate a distinct random
secret and put the same value in `R72_GROUP_HQ_CRM_TOKEN` on the Relaunch72 web
service. Do not reuse `PP_SERVICE_TOKEN`. The Relaunch72 service also needs its
fixed Property Predator organisation, workspace and active owner IDs in
`R72_GROUP_HQ_CRM_PP_ORGANIZATION_ID`, `R72_GROUP_HQ_CRM_PP_WORKSPACE_ID`, and
`R72_GROUP_HQ_CRM_OWNER_USER_ID`. Its profile must be `property_predator_growth`.
Partial configuration leaves the bridge unavailable without stopping the portal.

Release sequence: publish the Relaunch72 endpoint, run `/ready` on PP and MHP,
then publish Group HQ. Configure both secrets and the fixed IDs; neither token
is exposed to a browser. The founder signs into Group HQ and opens `/crm` to
check one owned test lead, its stage and its link to the R72 case file. Check
that an unauthorised request gets 401, an MHP brand query gets 400, a request
to the MHP host never reaches the PP endpoint, and a removed owner membership
gets 403. If the connection fails, remove `R72_CRM_TOKEN` from Group HQ; `/crm`
shows unavailable while the existing admin and product journeys continue.

This bridge is a read-only first slice. It does not capture new forms, trigger
LeadRelay calls or enable SMS/WhatsApp; each needs its own release checks.

The connector and template smoke checks run without a database:
`python -m unittest discover -s unit_checks -p '*.py' -v`. The existing
`tests/` suite still requires a disposable Postgres 16 database.
