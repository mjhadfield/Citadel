-- Wake-on-LAN for a Remote Access device: its network card's hardware (MAC) address. With one, a tile for a device
-- that's asleep or off wakes it instead of opening a viewer (app/wol.py). Blank = no wake.
ALTER TABLE remote_hosts ADD COLUMN mac TEXT NOT NULL DEFAULT '';
