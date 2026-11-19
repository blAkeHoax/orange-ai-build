// src/components/ChatView.jsx - Markdown-enabled rich chat timeline
import React, { useRef, useEffect } from 'react';
import { User, Loader2 } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import BrandTrigger from './BrandTrigger';
import Composer from './Composer';
import CodeBlock from './CodeBlock';
import ToolResultCard from './ToolResultCard';
import RecommendationCard from './RecommendationCard';
import { openExternalUrl } from '../services/tauriBridge';
import orbImg from '../assets/logo_orb_64.png';
import '../styles/ChatView.css';

// Markdown component mapping for semantic, secure typography
const markdownComponents = {
  code({ node, inline, className, children, ...props }) {
    const match = /language-(\w+)/.exec(className || '');
    const codeString = String(children || '').replace(/\n$/, '');

    // Block code (explicit language or contains multiline code)
    if (!inline && (match || String(children).includes('\n'))) {
      return (
        <CodeBlock
          language={match ? match[1] : ''}
          code={codeString}
        />
      );
    }

    // Inline code
    return (
      <code className="inline-code" {...props}>
        {children}
      </code>
    );
  },
  h1: ({ children }) => <h1 className="md-h1">{children}</h1>,
  h2: ({ children }) => <h2 className="md-h2">{children}</h2>,
  h3: ({ children }) => <h3 className="md-h3">{children}</h3>,
  p: ({ children }) => <p className="md-p">{children}</p>,
  ul: ({ children }) => <ul className="md-ul">{children}</ul>,
  ol: ({ children }) => <ol className="md-ol">{children}</ol>,
  li: ({ children }) => <li className="md-li">{children}</li>,
  blockquote: ({ children }) => <blockquote className="md-blockquote">{children}</blockquote>,
  hr: () => <hr className="md-hr" />,
  table: ({ children }) => (
    <div className="md-table-wrapper">
      <table className="md-table">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="md-thead">{children}</thead>,
  tbody: ({ children }) => <tbody className="md-tbody">{children}</tbody>,
  tr: ({ children }) => <tr className="md-tr">{children}</tr>,
  th: ({ children }) => <th className="md-th">{children}</th>,
  td: ({ children }) => <td className="md-td">{children}</td>,
  a: ({ href, children }) => {
    // Security check: validate protocol
    const isSafe = href && (href.startsWith('http://') || href.startsWith('https://'));
    const handleClick = (e) => {
      e.preventDefault();
      if (isSafe) {
        openExternalUrl(href);
      }
    };
    return (
      <a
        href={isSafe ? href : '#'}
        onClick={handleClick}
        className="chat-link"
        target="_blank"
        rel="noopener noreferrer"
        title={isSafe ? href : 'Blocked unsafe link'}
      >
        {children}
      </a>
    );
  }
};

export default function ChatView({
  onToggleDrawer,
  isDrawerOpen,
  messages = [],
  onSendMessage,
  isExecuting = false
}) {
  const messagesEndRef = useRef(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isExecuting]);

  return (
    <div className="chat-view-container">
      {/* Top Bar with Brand Trigger */}
      <div className="chat-top-bar">
        <BrandTrigger onToggleDrawer={onToggleDrawer} isDrawerOpen={isDrawerOpen} />
        <div className="chat-top-title">Active Conversation</div>
      </div>

      {/* Messages Timeline */}
      <div className="chat-timeline">
        {messages.length === 0 ? (
          <div className="chat-empty-state">
            <img src={orbImg} alt="" className="chat-empty-orb" />
            <div className="chat-empty-title">Orange AI Conversation</div>
            <div className="chat-empty-desc">
              Ask anything or issue a command. Your conversation history and context are preserved.
            </div>
          </div>
        ) : (
          messages.map((msg, idx) => (
            <div key={idx} className={`chat-message-row is-${msg.role}`}>
              <div className="chat-avatar">
                {msg.role === 'user' ? (
                  <User size={16} />
                ) : (
                  <img src={orbImg} alt="AI" className="chat-ai-avatar" />
                )}
              </div>
              <div className="chat-message-bubble">
                <div className="chat-message-sender">
                  {msg.role === 'user' ? 'You' : 'Orange AI'}
                  {msg.model && <span className="chat-model-tag">{msg.model}</span>}
                </div>

                {/* User plain text vs Assistant rich markdown */}
                {msg.role === 'user' ? (
                  <div className="chat-message-content is-user-text">{msg.text}</div>
                ) : (
                  <div className="chat-message-content is-assistant-content">
                    {msg.tool_result && (
                      <ToolResultCard {...msg.tool_result} />
                    )}
                    {msg.text && (
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm]}
                        components={markdownComponents}
                      >
                        {msg.text}
                      </ReactMarkdown>
                    )}
                    {msg.recommendation && (
                      <RecommendationCard recommendation={msg.recommendation} />
                    )}
                  </div>
                )}
              </div>
            </div>
          ))
        )}

        {isExecuting && (
          <div className="chat-message-row is-assistant is-thinking">
            <div className="chat-avatar">
              <img src={orbImg} alt="AI" className="chat-ai-avatar" />
            </div>
            <div className="chat-message-bubble">
              <div className="chat-thinking-indicator">
                <Loader2 size={16} className="spinner" />
                <span>Orange AI is thinking...</span>
              </div>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Floating Bottom Composer */}
      <Composer onSendMessage={onSendMessage} disabled={isExecuting} />
    </div>
  );
}
