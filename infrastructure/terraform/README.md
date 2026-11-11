# Terraform — blocked on Q-HOST-1

The folders here (`envs/{staging,production}`, `modules/{network,app_node,mta_node,dns}`)
are placeholders. No Terraform has been written, on purpose.

**Why:** Terraform is written against one provider's API, and no hosting
provider has been chosen. Spec §13 and §18 require *written* confirmation from
the provider before purchase (see [docs/OPEN_QUESTIONS.md](../../docs/OPEN_QUESTIONS.md)
Q-HOST-1):

- outbound email permitted under the provider's acceptable-use policy;
- port 25 egress available and unfiltered;
- dedicated static IPs with customer-controlled PTR (reverse DNS) records;
- the IPs' reputation history checked before acceptance;
- a defined abuse-handling process and contact.

Several large clouds block port 25 by default or refuse PTR control, which
would make the MTA tier undeployable. Writing modules before that is settled
would mean writing them twice.

## What already works without Terraform

Servers can be created by hand (or in the provider's console) and configured
with Ansible, which is complete:

1. Create the hosts and put their names/addresses in
   `infrastructure/ansible/inventories/<env>/hosts.yml` (currently example values).
2. Set each MTA IP's PTR record to the HELO name the stream uses (§10.1).
3. Publish SPF, DKIM, DMARC and the bounce MX, then run `make dns-check`.
4. Follow [docs/DEPLOYMENT.md](../../docs/DEPLOYMENT.md).

## What the modules should cover once a provider is chosen

| Module | Creates |
| --- | --- |
| `network` | Private network for app ↔ database ↔ MTA traffic; firewall: 25 out from MTAs only, 443 in to the app nodes only, the submission listener reachable from app nodes only (no open relay, §12) |
| `app_node` | App hosts (Docker, read-only containers per `deploy/docker-compose.prod.yml`) |
| `mta_node` | MTA hosts with one static IP per stream pool and PTR records matching `smtp_helo_name` (§10.1, §10.3) |
| `dns` | SPF, DKIM selectors, DMARC, return-path MX and tracking CNAME for each sending domain (Q-DNS-1 supplies the names) |

`envs/staging` must route all mail to a sink (§6.2). Production state belongs in
a remote backend with locking, never in this repository.
