<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
<xsl:output method="html" encoding="UTF-8" indent="yes"/>
<xsl:template match="/rss/channel">
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title><xsl:value-of select="title"/> — RSS feed</title>
<style>
:root{color-scheme:light}
body{margin:0;background:#f6f5f2;color:#1a1a1a;font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:40px 22px 60px}
.badge{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#9c2c2c;background:#f7ecec;border:1px solid #ecd9d9;border-radius:20px;padding:4px 12px;margin-bottom:14px}
h1{font-size:24px;margin:0 0 6px}
.desc{color:#555;margin:0 0 18px}
.note{font-size:13px;color:#5a5a5a;background:#fff;border:1px solid #e6e4df;border-radius:10px;padding:12px 15px;margin-bottom:26px}
.note a{color:#1f6feb;text-decoration:none}
.item{padding:14px 0;border-top:1px solid #e6e4df}
.item .t{display:block;font-size:16px;font-weight:600;color:#1a1a1a;text-decoration:none;line-height:1.4}
.item .t:hover{color:#1f6feb}
.item .m{font-size:12.5px;color:#8a8a8a;margin-top:4px}
.foot{margin-top:30px;font-size:12px;color:#a5a5a5}
</style></head><body><div class="wrap">
<div class="badge">RSS feed</div>
<h1><xsl:value-of select="title"/></h1>
<p class="desc"><xsl:value-of select="description"/></p>
<div class="note">This is a live RSS feed. To subscribe, copy this page's URL into a feed reader such as Feedly, Inoreader or Thunderbird. Or <a><xsl:attribute name="href"><xsl:value-of select="link"/></xsl:attribute>return to the monitor</a>.</div>
<xsl:for-each select="item">
<div class="item">
<a class="t"><xsl:attribute name="href"><xsl:value-of select="link"/></xsl:attribute><xsl:value-of select="title"/></a>
<div class="m"><xsl:value-of select="description"/></div>
</div>
</xsl:for-each>
<div class="foot">Updated <xsl:value-of select="lastBuildDate"/></div>
</div></body></html>
</xsl:template>
</xsl:stylesheet>
