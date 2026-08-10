Quillon Labs test rig scheduling is now the bottleneck

The Quillon Labs test rig has become the constraint on everything downstream of it, and the way
it is scheduled is making that worse rather than better.

There is one rig. Bookings are taken first come, first served, and a booking holds the rig for a
full day regardless of whether the work needs a full day. In practice that means a twenty-minute
check and a full replication run cost the same slot, and the twenty-minute checks are booked far
more often because they are easier to justify.

The people running long jobs have adapted by booking several consecutive days speculatively and
releasing what they do not use, which is rational and has made the calendar useless as a picture
of what is actually happening. Two people described the calendar as something they no longer
read.

What Quillon wants is a scheduling arrangement where slot length matches job length, with some
protection for the long jobs that would otherwise never win a first-come race. They have not
proposed a specific mechanism and were explicit that they would rather we did not arrive with
one — they want to describe the constraint properly first.

This came up while we were closing out the Northwind handover, which is the only reason it was
in that conversation at all. It is not a Northwind matter and nothing about the rig touches the
depot work; it is Quillon's own operational problem, raised in a meeting that happened to be
about something else.

Next step is a proper description of the rig's actual utilisation, which nobody currently has.
