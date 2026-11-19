// src/components/CodeBlock.jsx - Syntax-highlighted code container with copy button
import React, { useState } from 'react';
import { Copy, Check, Terminal, Code2 } from 'lucide-react';
import Prism from 'prismjs';
import 'prismjs/components/prism-python';
import 'prismjs/components/prism-javascript';
import 'prismjs/components/prism-typescript';
import 'prismjs/components/prism-bash';
import 'prismjs/components/prism-json';
import 'prismjs/components/prism-yaml';
import 'prismjs/components/prism-sql';
import 'prismjs/components/prism-rust';
import 'prismjs/components/prism-markdown';

// Language aliases mapping
const LANGUAGE_ALIASES = {
  py: 'python',
  js: 'javascript',
  ts: 'typescript',
  sh: 'bash',
  shell: 'bash',
  zsh: 'bash',
  yml: 'yaml',
  rs: 'rust',
  md: 'markdown',
  jsonc: 'json'
};

// Safe recursive token renderer - NO dangerouslySetInnerHTML!
function renderToken(token, index) {
  if (typeof token === 'string') {
    return token;
  }
  const content = Array.isArray(token.content)
    ? token.content.map((t, i) => renderToken(t, i))
    : (typeof token.content === 'object' && token.content !== null
        ? renderToken(token.content, 0)
        : token.content);

  const typeClass = token.type ? `token ${token.type}` : 'token';
  return (
    <span key={index} className={typeClass}>
      {content}
    </span>
  );
}

export default function CodeBlock({ language, code = '' }) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);

  const rawCode = typeof code === 'string' ? code : String(code || '');
  const cleanLang = (language || '').toLowerCase().trim();
  const normalizedLang = LANGUAGE_ALIASES[cleanLang] || cleanLang;

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(rawCode);
      setCopied(true);
      setCopyError(false);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.warn('Failed to copy code to clipboard:', err);
      setCopyError(true);
      setTimeout(() => setCopyError(false), 2500);
    }
  };

  // Syntax highlighting via Prism.tokenize (100% pure React nodes)
  let highlightedNodes = null;
  const grammar = Prism.languages[normalizedLang];
  if (grammar && rawCode) {
    try {
      const tokens = Prism.tokenize(rawCode, grammar);
      highlightedNodes = tokens.map((token, idx) => renderToken(token, idx));
    } catch (err) {
      console.warn('Prism tokenization fallback:', err);
      highlightedNodes = rawCode;
    }
  } else {
    highlightedNodes = rawCode;
  }

  const displayLang = normalizedLang || 'code';

  return (
    <div className="code-block-container">
      <div className="code-block-header">
        <div className="code-block-lang">
          {displayLang === 'bash' || displayLang === 'shell' ? (
            <Terminal size={13} className="code-lang-icon" />
          ) : (
            <Code2 size={13} className="code-lang-icon" />
          )}
          <span className="code-lang-name">{displayLang}</span>
        </div>
        <button
          className={`code-copy-btn ${copied ? 'is-copied' : ''} ${copyError ? 'is-error' : ''}`}
          onClick={handleCopy}
          type="button"
          title="Copy code to clipboard"
        >
          {copied ? (
            <>
              <Check size={13} />
              <span>Copied!</span>
            </>
          ) : copyError ? (
            <span>Copy failed</span>
          ) : (
            <>
              <Copy size={13} />
              <span>Copy</span>
            </>
          )}
        </button>
      </div>
      <div className="code-block-scroll-wrapper">
        <pre className="code-block-pre">
          <code className={`language-${normalizedLang || 'plaintext'}`}>
            {highlightedNodes}
          </code>
        </pre>
      </div>
    </div>
  );
}
