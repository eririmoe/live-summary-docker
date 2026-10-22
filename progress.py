"""Extract scalar transfer progress without exposing URLs or log lines."""
import re


def download_percent(text,expected_bytes):
    values=re.findall(r'(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%',text)
    for value in reversed(values):
        if 0<=float(value)<=100:
            return float(value)
    units={'B':1,'KB':1024,'MB':1024**2,'GB':1024**3,'TB':1024**4}
    for line in reversed(text.splitlines()):
        values=re.findall(r'(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB|B)(/s)?',line)
        sizes=[float(n)*units[unit] for n,unit,rate in values if not rate]
        if len(sizes)>=2:
            received,total=sizes[:2]
            if expected_bytes>0 and abs(total-expected_bytes)<=max(1024,expected_bytes*0.02) and 0<=received<=total:
                return round(min(99.9,100*received/total),1)
    return None
