"""Partner simulators for the dev environment.

They stand in for the companies around the control tower and run as their own Container Apps
with their own identity and their own data, talking to the control tower only over HTTPS:

  sim.corpa  Corp A: the MCF fulfillment API (OAuth2, orders, inventory, tracking, cases) and the
             warehouse/carrier behaviour behind it, which pushes signed webhooks back.
  sim.corpb  Corp B: the order management system that sends consumer orders, receives order
             updates, and drops weekly forecast and sales files into the landing storage.
  sim.corpc  Corp C: a home goods chain, the second retail client. Its website sends consumer orders
             and its twelve stores send weekly case replenishment orders against the same stock.

Nothing in tower/ imports this package; the contract between them is docs/PARTNER_APIS.md.
"""
