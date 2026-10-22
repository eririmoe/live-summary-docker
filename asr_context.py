"""Reviewed room vocabulary for new Filetrans submissions only."""
import hashlib,json
import core


def enhancement(cfg, profile_path=None):
    if not cfg.get('asr_context_enabled',False):
        return {'input':{},'parameters':{},'profile':{'enabled':False}}
    if cfg['asr_model']!='qwen-audio-3.1-asr-flash-filetrans':
        raise ValueError('unsupported_model')
    value=core.read_json(profile_path or core.APP/'asr-vocabulary.json')
    if not isinstance(value,dict) or not isinstance(value.get('terms'),list):
        raise ValueError('profile_missing')
    vocabulary={}
    for item in value['terms']:
        word=item['word'];weight=item['weight']
        if not isinstance(word,str) or not word.strip() or word!=word.strip() or word in vocabulary:
            raise ValueError('word_invalid_or_duplicate')
        if (not word.isascii() and len(word)>15) or (word.isascii() and len(word.split())>7):
            raise ValueError('word_exceeds_provider_limit')
        if type(weight) is not int or not 1<=weight<=5:
            raise ValueError('weight_invalid')
        vocabulary[word]=weight
    if not 1<=len(vocabulary)<=2000:
        raise ValueError('word_count_invalid')
    text=value['context_prefix']+'；常用词：'+'、'.join(vocabulary)
    if len(text)>400:
        raise ValueError('context_exceeds_provider_limit')
    context=[{'role':'user','content':[{'type':'input_text','text':text}]}]
    content={'context':context,'vocabulary':vocabulary}
    digest=hashlib.sha256(json.dumps(content,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return {'input':{'context':context},'parameters':{'vocabulary':vocabulary},
        'profile':{'enabled':True,'version':value['version'],'sha256':digest,
            'term_count':len(vocabulary),'context_characters':len(text),**content}}
