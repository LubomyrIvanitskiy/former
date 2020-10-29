import sys
from gensim.corpora import WikiCorpus

def tokenize(content, token_min_len, token_max_len, lower):
    #override original method in wikicorpus.py
    return [token.encode('utf8').lower() if lower else token.encode('utf8') for token in content.split()
           if len(token) <= token_max_len and not token.startswith('_')]

def make_corpus(in_f, out_f):
    """Convert Wikipedia xml dump file to text corpus"""

    output = open(out_f, 'w')
    # https://radimrehurek.com/gensim/corpora/wikicorpus.html
    # https://stackoverflow.com/questions/50697092/how-to-get-the-wikipedia-corpus-text-with-punctuation-by-using-gensim-wikicorpus
    wiki = WikiCorpus("wiki_dump", lower=False, tokenizer_func=tokenize)

    i = 0
    for i, text in enumerate(wiki.get_texts()):
      text = ' '.join([t.decode("utf-8") for t in text])
      output.write(bytes(text, 'utf-8').decode('utf-8') + '\n')
      if (i % 10000 == 0):
          print('Processed ' + str(i) + ' articles')
    output.close()
    print('Processing complete!')


if __name__ == '__main__':

    if len(sys.argv) != 3:
        print('Usage: python make_wiki_corpus.py <wikipedia_dump_file> <processed_text_file>')
        sys.exit(1)
    in_f = sys.argv[1]
    out_f = sys.argv[2]
    make_corpus(in_f, out_f)
v
