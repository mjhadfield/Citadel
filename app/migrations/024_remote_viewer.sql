-- Which program a Remote Access tile opens on this PC. KRDC scales the remote screen to its window; TigerVNC's
-- vncviewer can't, so a 4K screen came through at full size. TigerVNC stays for servers KRDC can't log in to: the
-- Pis' wayvnc offers only X509 and RSA-AES logins ("VNC authentication type is unsupported" in KRDC).
ALTER TABLE remote_hosts ADD COLUMN viewer TEXT NOT NULL DEFAULT 'krdc' CHECK (viewer IN ('krdc', 'tigervnc'));
-- Tiles that existed before this were all TigerVNC; the Pi ones stay that way (Mike, 2026-11-17), the rest move to KRDC.
UPDATE remote_hosts SET viewer = 'tigervnc' WHERE lower(address) LIKE 'pi-%' OR lower(name) LIKE 'pi-%' OR lower(name) LIKE 'pi %';
