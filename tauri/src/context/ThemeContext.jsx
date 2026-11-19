// src/context/ThemeContext.jsx - Comprehensive 3-Mode Theme Provider (Dark, Light, System)
import React, { createContext, useContext, useState, useEffect, useCallback } from 'react';

const ThemeContext = createContext();

function getSystemTheme() {
  if (typeof window !== 'undefined' && window.matchMedia) {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  return 'dark';
}

export function ThemeProvider({ children }) {
  // 1. User's saved preference: 'dark' | 'light' | 'system'
  const [themePreference, setThemePreferenceState] = useState(() => {
    try {
      const saved = localStorage.getItem('orange_ai_theme_preference');
      if (saved === 'light' || saved === 'dark' || saved === 'system') {
        return saved;
      }
      const legacyTheme = localStorage.getItem('orange_ai_theme');
      return legacyTheme === 'light' ? 'light' : 'dark';
    } catch {
      return 'dark';
    }
  });

  // 2. Computed / Resolved theme actually applied to CSS: 'dark' | 'light'
  const [resolvedTheme, setResolvedTheme] = useState(() => {
    if (themePreference === 'system') {
      return getSystemTheme();
    }
    return themePreference;
  });

  // 3. React to OS preference changes when themePreference is 'system'
  useEffect(() => {
    if (themePreference !== 'system') {
      setResolvedTheme(themePreference);
      return;
    }

    // Set initial system theme
    setResolvedTheme(getSystemTheme());

    if (!window.matchMedia) return;
    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');

    const handleMediaChange = (e) => {
      setResolvedTheme(e.matches ? 'dark' : 'light');
    };

    if (mediaQuery.addEventListener) {
      mediaQuery.addEventListener('change', handleMediaChange);
    } else if (mediaQuery.addListener) {
      mediaQuery.addListener(handleMediaChange);
    }

    return () => {
      if (mediaQuery.removeEventListener) {
        mediaQuery.removeEventListener('change', handleMediaChange);
      } else if (mediaQuery.removeListener) {
        mediaQuery.removeListener(handleMediaChange);
      }
    };
  }, [themePreference]);

  // 4. Apply resolved theme to DOM
  useEffect(() => {
    try {
      localStorage.setItem('orange_ai_theme_preference', themePreference);
      localStorage.setItem('orange_ai_theme', resolvedTheme);
    } catch (e) {
      console.warn('Failed to save theme to localStorage:', e);
    }
    document.documentElement.setAttribute('data-theme', resolvedTheme);
    document.body.setAttribute('data-theme', resolvedTheme);
  }, [themePreference, resolvedTheme]);

  // 5. Explicit Setters
  const setThemePreference = useCallback((mode) => {
    if (mode === 'dark' || mode === 'light' || mode === 'system') {
      setThemePreferenceState(mode);
    }
  }, []);

  const setTheme = useCallback((mode) => {
    // Backward-compatible setter
    setThemePreference(mode);
  }, [setThemePreference]);

  const toggleTheme = useCallback(() => {
    setThemePreferenceState((prev) => (prev === 'dark' ? 'light' : 'dark'));
  }, []);

  // 6. Test & Automation Bridge on window.__orangeAiTheme
  useEffect(() => {
    window.__orangeAiTheme = {
      theme: resolvedTheme,
      resolvedTheme,
      themePreference,
      toggleTheme,
      setTheme: (t) => setTheme(t),
      setThemePreference: (t) => setThemePreference(t),
      getTheme: () => resolvedTheme,
      getThemePreference: () => themePreference,
      getSystemTheme
    };
    return () => {
      delete window.__orangeAiTheme;
    };
  }, [resolvedTheme, themePreference, toggleTheme, setTheme, setThemePreference]);

  return (
    <ThemeContext.Provider
      value={{
        theme: resolvedTheme, // Backward compatibility
        resolvedTheme,
        themePreference,
        toggleTheme,
        setTheme,
        setThemePreference
      }}
    >
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  const context = useContext(ThemeContext);
  if (!context) {
    throw new Error('useTheme must be used within a ThemeProvider');
  }
  return context;
}
