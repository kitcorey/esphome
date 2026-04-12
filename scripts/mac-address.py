#!/usr/bin/env python
import random
oid = "50:C7:BF"
# Generate a random MAC address with the given OID prefix
new_mac = oid + ":{:02X}:{:02X}:{:02X}".format(random.randint(0,255), random.randint(0,255), random.randint(0,255))
print(new_mac)
