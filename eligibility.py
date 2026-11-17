"""Recognise explicit student-only restrictions in property-specific text."""
import re
from bs4 import BeautifulSoup

STUDENT_CHECK_VERSION = 1
_PERSON = r'(?:\d+|een|twee|drie|vier|a|an|two|three|four|maximaal|maximum|max|international(?:e)?|ingeschreven|registered|enrolled|full[- ]time|voltijd|studerende)'
_STUDENT = r'(?:student(?:en|s)?|studerenden)\b'
_PATTERNS = [
    rf'\b(?:alleen|uitsluitend|enkel|only)\s+(?:{_PERSON}\s+){{0,4}}{_STUDENT}',
    rf'\b(?:alleen|uitsluitend|enkel|exclusief|slechts)\s+(?:(?:beschikbaar|bestemd|geschikt|toegankelijk|aangeboden)\s+)?(?:voor|aan)\s+(?:{_PERSON}\s+){{0,4}}{_STUDENT}',
    rf'\b(?:alleen|uitsluitend|enkel|exclusief)\s+(?:worden\s+)?verhuurd\s+aan\s+(?:{_PERSON}\s+){{0,4}}{_STUDENT}',
    rf'\b(?:only|exclusively|solely|strictly)\s+(?:(?:available|suitable|intended|reserved|offered)\s+)?(?:for|to)\s+(?:{_PERSON}\s+){{0,4}}{_STUDENT}',
    rf'\b(?:available|reserved|intended)\s+(?:only|exclusively)\s+for\s+(?:{_PERSON}\s+){{0,4}}{_STUDENT}',
    rf'\b(?:uitsluitend|alleen|enkel)\s+te\s+huur\s+(?:voor|aan)\s+(?:{_PERSON}\s+){{0,4}}{_STUDENT}',
    rf'\b(?:only|exclusively)\s+(?:be\s+)?(?:rented|let|offered)\s+(?:to|by)\s+(?:{_PERSON}\s+){{0,4}}{_STUDENT}',
    r'\bstudents?[- ]only\b',
    rf'\b(?:must|have\s+to)\s+be\s+(?:{_PERSON}\s+){{0,4}}{_STUDENT}',
    rf'\b(?:moet|moeten|dient|dienen)\s+(?:{_PERSON}\s+){{0,4}}{_STUDENT}\s+(?:te\s+)?zijn\b',
    r'\b(?:studentenstatus|studenteninschrijving|student\s+status)\s+(?:is\s+)?(?:verplicht|required|mandatory)\b',
]
_NEGATED = re.compile(r'\b(?:niet|not)\s+(?:a\s+|an\s+|een\s+)?(?:alleen|uitsluitend|enkel|exclusief|only|exclusively|solely|students?[- ]only)\b', re.I)
_MIXED = re.compile(r'\bstudent(?:s|en)?\s*[,/]?\s*(?:en|of|and|or|&)\s+(?:(?:young|jonge|werkende)\s+)?(?:professionals?|werkenden|werknemers|starters|expats?|stagiairs?|interns?|non[- ]students?|niet[- ]studenten)\b', re.I)


def student_restriction(text):
    text = BeautifulSoup(str(text or ''), 'html.parser').get_text(' ', strip=True)
    for clause in re.split(r'[.!?;\n|]+', text):
        clause = re.sub(r'\s+', ' ', clause).strip()
        # "Not only students" and "students or professionals" are not exclusive.
        if _MIXED.search(clause):
            continue
        for pattern in _PATTERNS:
            for match in re.finditer(pattern, clause, re.I):
                if any(neg.start() <= match.start() < neg.end() for neg in _NEGATED.finditer(clause)):
                    continue
                return clause[max(0, match.start()-30):match.end()+120][:220]
    return ''


def student_assessment(text):
    evidence = student_restriction(text)
    return {'studentOnly': bool(evidence), 'studentRestriction': evidence,
            'studentCheckVersion': STUDENT_CHECK_VERSION}


def listing_page_text(html):
    soup = BeautifulSoup(html, 'html.parser')
    # At Home puts server-rendered property markup in a Vue inline template.
    # Reparse its contents so Beautiful Soup treats the text as normal page text.
    for template in soup.find_all('template'):
        template.replace_with(BeautifulSoup(template.decode_contents(), 'html.parser'))
    for tag in soup.select('script, style, nav, header, footer, aside, [class*="related"], [class*="similar"], [class*="relevant-properties"]'):
        tag.decompose()
    root = soup.select_one('main') or soup.body or soup
    return root.get_text(' ', strip=True)


def at_home_restrictions(html, url):
    text = listing_page_text(html)
    if len(text) < 60:
        raise ValueError('At Home property description could not be read')
    return student_assessment(text)


def property_text(value):
    """Read description/terms fields inside one property, excluding global app data."""
    def flatten(part):
        if isinstance(part, str): return part
        if isinstance(part, dict): return ' '.join(flatten(v) for v in part.values())
        if isinstance(part, list): return ' '.join(flatten(v) for v in part)
        return ''
    result = []
    def visit(part):
        if isinstance(part, dict):
            for key, val in part.items():
                if any(word in key.casefold() for word in ('description', 'condition', 'remark', 'restriction', 'requirement', 'title', 'omschrijving', 'voorwaard')):
                    result.append(flatten(val))
                elif isinstance(val, (dict, list)):
                    visit(val)
        elif isinstance(part, list):
            for val in part: visit(val)
    visit(value)
    return ' '.join(result)
