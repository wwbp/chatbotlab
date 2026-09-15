import React, { useState, useEffect } from 'react';
import MessageList from './MessageList';
import ChatInput from './ChatInput';
import '../styles/Conversation.css';

const Conversation = () => {
  const [message, setMessage] = useState('');
  const [messages, setMessages] = useState([]);
  const [isTyping, setIsTyping] = useState(false);
  const [avatar, setAvatar] = useState({
    bot_id: '',
    bot_name: '',
    avatar_type: 'none',
    image_base64: '',
  });
  const [botConfig, setBotConfig] = useState(null);
  // const [idleTimer, setIdleTimer] = useState(null);
  // const [lastUserActivity, setLastUserActivity] = useState(Date.now());
  const [, setFollowupRequested] = useState(false);

  const apiUrl = import.meta.env.VITE_API_URL || '/api';
  const params = new URLSearchParams(window.location.search);
  const botName = params.get('bot_name');
  const conversationId = params.get('conversation_id');
  const participantId = params.get('participant_id');
  const surveyId = params.get('survey_id') || '';
  const studyName = params.get('study_name') || '';
  const userGroup = params.get('user_group') || '';
  const condition = params.get('condition') || '';
  const surveyMetaData = window.location.href;

  // Initialize conversation on mount.
  // bot_config comes from the init response — no separate GET /api/bots/ call needed.
  useEffect(() => {
    if (!botName || !participantId) return;

    const initConv = async () => {
      try {
        const res = await fetch(`${apiUrl}/initialize_conversation/`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            bot_name: botName,
            conversation_id: conversationId,
            participant_id: participantId,
            study_name: studyName,
            user_group: userGroup,
            survey_id: surveyId,
            survey_meta_data: surveyMetaData,
          }),
        });
        if (!res.ok) throw new Error((await res.json()).error || 'Init failed');
        const data = await res.json();

        if (data.bot_config) {
          setBotConfig(data.bot_config);
        }

        let avatar_data;
        try {
          const query = new URLSearchParams({
            conversation_id: conversationId,
            condition: condition,
          });
          const avatar_response = await fetch(
            `${apiUrl}/avatar/${botName}/?${query}`
          );
          if (!avatar_response.ok) {
            throw new Error(`Failed to get image`);
          }
          avatar_data = await avatar_response.json();
        } catch (avatarErr) {
          // console.warn('Failed to fetch avatar. Using none.');
          avatar_data = {
            image_url: null, // <-- your default image path
            bot_id: '',
            bot_name: '',
            avatar_type: 'none',
          };
        }

        if (data.initial_utterance?.trim()) {
          setAvatar(avatar_data);
          setMessages([
            { sender: 'AI Chatbot', content: data.initial_utterance },
          ]);
        }

        // Handle existing messages if conversation was already created
        if (data.existing_messages && data.existing_messages.length > 0) {
          setAvatar(avatar_data);
          setMessages(data.existing_messages);
        }
      } catch (err) {
        // console.error('Failed to initialize conversation:', err);
      }
    };
    initConv();
  }, [
    apiUrl,
    botName,
    conversationId,
    participantId,
    studyName,
    surveyId,
    userGroup,
    surveyMetaData,
  ]);

  // Idle detection and follow-up logic - only based on conversation data
  useEffect(() => {
    if (!botConfig?.follow_up_on_idle || !botConfig?.idle_time_minutes) {
      return;
    }

    let currentTimer = null;
    let isFollowupRequested = false;

    // const resetIdleTimer = () => {
    //   if (currentTimer) {
    //     clearTimeout(currentTimer);
    //     currentTimer = null;
    //   }
    //   isFollowupRequested = false;
    //   setFollowupRequested(false);
    // };

    const startIdleTimer = () => {
      const idleTimeMs = botConfig.idle_time_minutes * 60 * 1000;
      currentTimer = setTimeout(async () => {
        // Prevent multiple followup requests
        if (isFollowupRequested) {
          // console.log('🕐 Followup already requested, skipping...');
          return;
        }

        try {
          // console.log('🕐 User idle detected, requesting follow-up...');
          isFollowupRequested = true;
          setFollowupRequested(true);

          const res = await fetch(`${apiUrl}/followup/`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              bot_name: botName,
              conversation_id: conversationId,
              participant_id: participantId,
            }),
          });

          if (res.ok) {
            const data = await res.json();
            const chunks = data.response_chunks || [data.response];
            const useHumanlikeDelay = data.humanlike_delay !== false; // Default to true if not specified
            const delayConfig = data.delay_config || null;
            // console.log(
            //   `📝 Follow-up response has ${chunks.length} chunks, humanlike delay: ${useHumanlikeDelay}`,
            //   delayConfig
            // );

            // Handle new delay system for followup
            if (delayConfig && delayConfig.response_segments) {
              // New delay system
              executeTypingSequence(
                delayConfig.response_segments,
                delayConfig,
                0
              );
            } else {
              // Legacy delay system (backward compatibility)
              setIsTyping(true);
              revealChunks(chunks, 0, useHumanlikeDelay, delayConfig);
            }
          } else {
            // const error = await res.json();
            // console.warn('Follow-up request failed:', error.error);
            isFollowupRequested = false;
            setFollowupRequested(false); // Reset flag on error
          }
        } catch (err) {
          // console.error('Error requesting follow-up:', err);
          isFollowupRequested = false;
          setFollowupRequested(false); // Reset flag on error
        }
      }, idleTimeMs);
    };

    // Start initial timer
    startIdleTimer();

    // Cleanup
    return () => {
      if (currentTimer) {
        clearTimeout(currentTimer);
      }
    };
  }, [
    botConfig,
    botName,
    conversationId,
    participantId,
    apiUrl,
    messages.length,
  ]);

  const getHumanDelay = (
    chunk,
    chunkIndex,
    totalChunks,
    backendTimeMs,
    delayConfig = null
  ) => {
    // Use bot-specific config or fallback to defaults
    const config = delayConfig || {
      typing_speed_min_ms: 100,
      typing_speed_max_ms: 200,
      question_thinking_ms: 300,
      first_chunk_thinking_ms: 600,
      last_chunk_pause_ms: 100,
      min_delay_ms: 200,
      max_delay_ms: 800,
    };

    // Base typing speed: configurable ms per character
    const baseTypingTime =
      chunk.length *
      (Math.random() *
        (config.typing_speed_max_ms - config.typing_speed_min_ms) +
        config.typing_speed_min_ms);

    // Contextual adjustments (configurable)
    let contextualDelay = 0;
    if (chunk.includes('?')) contextualDelay += config.question_thinking_ms;
    if (chunkIndex === 0) contextualDelay += config.first_chunk_thinking_ms;
    if (chunkIndex === totalChunks - 1)
      contextualDelay += config.last_chunk_pause_ms;

    const totalDelay = baseTypingTime + contextualDelay;

    // Smart backend compensation with configurable limits
    if (backendTimeMs >= totalDelay) {
      // Backend was slow, use minimum delays to maintain smoothness
      const minDelay = Math.max(
        config.min_delay_ms,
        config.max_delay_ms - (backendTimeMs - totalDelay) / totalChunks
      );
      // console.log(
      //   `🐌 Slow backend (${backendTimeMs}ms), using min delay: ${minDelay}ms for chunk ${chunkIndex + 1}/${totalChunks}`
      // );
      return minDelay;
    } else {
      // Backend was fast, subtract its time from our delay
      const adjustedDelay = Math.max(
        config.min_delay_ms,
        totalDelay - backendTimeMs
      );
      // console.log(
      //   `✅ Normal timing: base=${totalDelay}ms, backend=${backendTimeMs}ms, adjusted=${adjustedDelay}ms for chunk ${chunkIndex + 1}/${totalChunks}`
      // );
      return adjustedDelay;
    }
  };

  // Reveal chunks one by one
  const revealChunks = (
    chunks,
    backendTimeMs = 0,
    useHumanlikeDelay = true,
    delayConfig = null
  ) => {
    const valid = chunks.filter(c => typeof c === 'string' && c.trim().length);

    if (!valid.length) {
      setIsTyping(false);
      return;
    }

    // If humanlike delay is disabled, show all chunks instantly
    if (!useHumanlikeDelay) {
      valid.forEach(chunk => {
        setMessages(prev => [
          ...prev,
          { sender: 'AI Chatbot', content: chunk },
        ]);
      });
      setIsTyping(false);
      return;
    }

    // Apply humanlike delays
    let cumulative = 0;
    const totalChunks = valid.length;

    valid.forEach((chunk, i) => {
      const delay = getHumanDelay(
        chunk,
        i,
        totalChunks,
        backendTimeMs,
        delayConfig
      );
      cumulative += delay;

      setTimeout(() => {
        setMessages(prev => [
          ...prev,
          { sender: 'AI Chatbot', content: chunk },
        ]);

        if (i === valid.length - 1) {
          setIsTyping(false);
        }
      }, cumulative);
    });
  };

  // Execute typing sequence with realistic delays
  const executeTypingSequence = (
    responseSegments,
    delayConfig,
    backendTimeMs
  ) => {
    const { reading_time, min_reading_delay } = delayConfig;

    // Calculate actual reading time (frontend handles backend latency)
    const backendLatencySeconds = backendTimeMs / 1000;
    const effectiveReadingTime = Math.max(
      min_reading_delay,
      reading_time - backendLatencySeconds
    );

    // Start reading delay
    setTimeout(() => {
      displayResponseSegments(responseSegments);
    }, effectiveReadingTime * 1000);
  };

  // Display response segments with realistic typing delays
  const displayResponseSegments = responseSegments => {
    let cumulativeDelay = 0;

    responseSegments.forEach((segment, index) => {
      const { content, writing_delay, inter_segment_delay } = segment;

      // Show typing indicator
      setTimeout(() => {
        setIsTyping(true);
      }, cumulativeDelay * 1000);

      // Hide typing indicator and show message
      setTimeout(
        () => {
          setIsTyping(false);
          setMessages(prev => [
            ...prev,
            {
              sender: 'AI Chatbot',
              content: content,
            },
          ]);
        },
        (cumulativeDelay + writing_delay) * 1000
      );

      cumulativeDelay += writing_delay + inter_segment_delay;
    });
  };

  // Handle user send
  const handleSubmit = async e => {
    e.preventDefault();
    if (!message.trim()) {
      alert('Please enter a message.');
      return;
    }
    // console.log('✉️ Enqueue user message:', message);

    // Reset followup timer when user sends a message
    setFollowupRequested(false); // Reset followup flag when user sends message

    // Reset the "followup sent once" flag when user sends a message
    // This allows followup to trigger again after user interaction
    if (botConfig?.follow_up_on_idle && !botConfig?.recurring_followup) {
      // Clear the server-side flag that prevents recurring followups
      fetch(`${apiUrl}/followup/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          bot_name: botName,
          conversation_id: conversationId,
          participant_id: participantId,
          reset_flag: true, // Signal to reset the "sent once" flag
        }),
      }).catch(() => {
        // console.log('Failed to reset followup flag:', err);
      });
    }

    setMessages(prev => [...prev, { sender: 'You', content: message }]);
    setMessage('');

    const requestStartTime = Date.now();

    try {
      const res = await fetch(`${apiUrl}/chatbot/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message,
          bot_name: botName,
          conversation_id: conversationId,
          participant_id: participantId,
        }),
      });
      if (!res.ok) {
        const err = await res.json();
        alert(`Error: ${err.error || 'Something went wrong'}`);
        setIsTyping(false);
        return;
      }
      const data = await res.json();
      const requestEndTime = Date.now();
      const backendTimeMs = requestEndTime - requestStartTime;

      // console.log(`⏱️ Backend request took ${backendTimeMs}ms`);

      const chunks = data.response_chunks || [data.response];
      const useHumanlikeDelay = data.humanlike_delay !== false; // Default to true if not specified
      const chunkMessages = data.chunk_messages !== false; // Default to true if not specified
      const delayConfig = data.delay_config || null;

      // console.log(
      //   `📝 Response has ${chunks.length} chunks, humanlike delay: ${useHumanlikeDelay}, chunk messages: ${chunkMessages}`,
      //   delayConfig
      // );

      // Handle new delay system
      if (delayConfig && delayConfig.response_segments) {
        // New delay system
        executeTypingSequence(
          delayConfig.response_segments,
          delayConfig,
          backendTimeMs
        );
      } else {
        // Legacy delay system (backward compatibility)
        const readingDelayMs = delayConfig?.reading_delay_ms || 0;
        setTimeout(() => {
          if (useHumanlikeDelay) {
            setIsTyping(true);
          }
          revealChunks(chunks, backendTimeMs, useHumanlikeDelay, delayConfig);
        }, readingDelayMs);
      }
    } catch (err) {
      // console.error('Error sending message:', err);
      alert('An error occurred. Please try again.');
      setIsTyping(false);
    }
  };

  return (
    <div className="text-conversation">
      <div className="conversation-container">
        <div className="chat-box">
          <MessageList
            messages={messages}
            isTyping={isTyping}
            avatar={avatar}
          />
          <ChatInput
            message={message}
            setMessage={setMessage}
            handleSubmit={handleSubmit}
          />
        </div>
      </div>
    </div>
  );
};

export default Conversation;
