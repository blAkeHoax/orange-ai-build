import React, { useState, useRef } from 'react';
import { Paperclip, Mic, SendHorizonal } from 'lucide-react';
import { useTheme } from '../context/ThemeContext';
import '../styles/Composer.css';

export default function Composer({ onSendMessage, disabled = false }) {
  const [inputText, setInputText] = useState('');
  const { theme } = useTheme();
  const inputRef = useRef(null);

  const placeholder =
    theme === 'light' ? 'Type a message to Orange AI...' : 'Type a message...';

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleSubmit = (e) => {
    if (e) e.preventDefault();
    const trimmed = inputText.trim();
    if (!trimmed || disabled) return;
    onSendMessage(trimmed);
    setInputText('');
  };

  const handleAttachmentClick = () => {
    // Stage 3 visual shell placeholder
    console.log('[Composer] Attachment button clicked');
  };

  const handleMicClick = () => {
    // Stage 3 visual shell placeholder
    console.log('[Composer] Microphone button clicked');
  };

  return (
    <div className="composer-container">
      <form className="composer-form" onSubmit={handleSubmit}>
        <div className="composer-pill">
          {/* Attachment Paperclip Button */}
          <button
            className="composer-action-btn btn-attachment"
            type="button"
            onClick={handleAttachmentClick}
            title="Attach file"
          >
            <Paperclip size={18} />
          </button>

          {/* Input Field */}
          <input
            ref={inputRef}
            className="composer-input"
            type="text"
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={placeholder}
            disabled={disabled}
            autoFocus
          />

          {/* Microphone Button */}
          <button
            className="composer-action-btn btn-mic"
            type="button"
            onClick={handleMicClick}
            title="Voice input"
          >
            <Mic size={18} />
          </button>
        </div>

        {/* Separate Orange Glowing Send Button */}
        <button
          className={`composer-send-btn ${inputText.trim() ? 'is-ready' : ''}`}
          type="submit"
          disabled={!inputText.trim() || disabled}
          title="Send message"
        >
          <SendHorizonal size={18} className="send-icon" />
        </button>
      </form>
    </div>
  );
}
