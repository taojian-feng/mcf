"""Partner simulators for the dev environment.

They stand in for the two companies around the MCF system and run as their own Container Apps
with their own identity and their own data, talking to the MCF system only over HTTPS:

  sim.corpa  Corp A: the MCF fulfillment API (OAuth2, orders, inventory, tracking, cases) and the
             warehouse/carrier behaviour behind it, which pushes signed webhooks back.
  sim.corpb  Corp B: the order management system that sends consumer orders, receives order
             updates, and drops weekly forecast and sales files into the landing storage.

Nothing in tower/ imports this package; the contract between them is docs/PARTNER_APIS.md.
"""
