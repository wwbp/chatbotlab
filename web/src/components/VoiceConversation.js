import React, { useState, useEffect, useRef } from 'react';
import '../styles/VoiceConversation.css';

const VoiceConversation = () => {
  const [, setIsConnected] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isTyping, setIsTyping] = useState(false);

  const pcRef = useRef(null);
  const dcRef = useRef(null);
  const audioRef = useRef(null);

  const apiUrl = import.meta.env.VITE_API_URL || '/api';
  const searchParams = new URLSearchParams(window.location.search);
  const botName = searchParams.get('bot_name');
  const conversationId = searchParams.get('conversation_id');
  const participantId = searchParams.get('participant_id');

  const surveyId = searchParams.get('survey_id') || '';
  const studyName = searchParams.get('study_name') || '';
  const userGroup = searchParams.get('user_group') || '';
  const surveyMetaData = window.location.href;

  const saveUtterance = async ({ text, isAssistant = false }) => {
    const formData = new FormData();
    formData.append('transcript', text);
    formData.append('conversation_id', conversationId);
    formData.append('is_voice', 'true');

    if (isAssistant) {
      formData.append('bot_name', botName);
    } else {
      formData.append('participant_id', participantId);
    }

    try {
      const res = await fetch(`${apiUrl}/upload_voice_utterance/`, {
        method: 'POST',
        body: formData,
      });
      // console.log('🔍 Uploading to:', `${apiUrl}/upload_voice_utterance/`);
      // const data = await res.json();
      // console.log('✅ Saved utterance:', data);
      await res.json(); // Keep the response to avoid unused variable warning
    } catch (err) {
      // console.error('❌ Failed to save utterance:', err);
    }
  };

  useEffect(() => {
    if (!botName || !participantId) return;

    const init = async () => {
      try {
        await fetch(`${apiUrl}/initialize_conversation/`, {
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
        // console.log('✅ Conversation initialized');
      } catch (err) {
        // console.error('Failed to initialize conversation:', err);
      }
    };

    init();
  }, [botName, participantId]);

  const startVoiceConversation = async () => {
    try {
      const sessionRes = await fetch(`${apiUrl}/session/`);
      const { client_secret } = await sessionRes.json();

      const pc = new RTCPeerConnection();
      pcRef.current = pc;

      const audioEl = document.createElement('audio');
      audioEl.autoplay = true;
      document.body.appendChild(audioEl);
      audioRef.current = audioEl;

      pc.ontrack = e => {
        audioEl.srcObject = e.streams[0];
      };

      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach(track => pc.addTrack(track, stream));

      const dc = pc.createDataChannel('oai-events');
      dcRef.current = dc;

      // let assistantBuffer = '';

      dc.onopen = () => {
        const enableTranscription = {
          type: 'session.update',
          session: {
            input_audio_transcription: {
              model: 'whisper-1',
            },
          },
        };
        dc.send(JSON.stringify(enableTranscription));
        // console.log(
        //   '📡 Sent session.update to enable user speech transcription'
        // );
      };

      dc.onmessage = event => {
        const message = JSON.parse(event.data);
        // console.log('📨 Message:', message);

        if (
          message.type ===
          'conversation.item.input_audio_transcription.completed'
        ) {
          const transcript = message.transcript?.trim();
          if (transcript) {
            // console.log('🗣️ USER FINAL TRANSCRIPT:', transcript);
            saveUtterance({ text: transcript });
            setIsTyping(true);
          }
        }

        if (message.type === 'response.content_part') {
          const partial = message.part?.transcript;
          if (partial) {
            // assistantBuffer += partial + ' ';
          }
        }

        if (message.type === 'response.content_part.done') {
          const finalText = message.part?.transcript?.trim();
          // console.log('🤖 Final Assistant Message:', finalText);
          saveUtterance({ text: finalText, isAssistant: true });
          // assistantBuffer = '';
          setIsTyping(false);
        }
      };

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);

      const sdpResponse = await fetch(
        'https://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17',
        {
          method: 'POST',
          body: offer.sdp,
          headers: {
            Authorization: `Bearer ${client_secret.value}`,
            'Content-Type': 'application/sdp',
          },
        }
      );

      const answer = { type: 'answer', sdp: await sdpResponse.text() };
      await pc.setRemoteDescription(answer);

      setIsConnected(true);
      setIsStreaming(true);
    } catch (err) {
      // console.error('❌ Failed to start voice session:', err);
    }
  };

  const stopVoiceConversation = () => {
    if (pcRef.current) pcRef.current.close();
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.srcObject = null;
      audioRef.current.remove();
    }
    setIsConnected(false);
    setIsStreaming(false);
  };

  return (
    <div className="conversation-container">
      <div className="chat-box">
        <div className="voice-status">
          {isStreaming ? (
            <p className="status-text">🎤 Listening...</p>
          ) : (
            <p className="status-text">Press the button to start talking</p>
          )}
          {isTyping && (
            <div className="typing-indicator">
              <span className="dot"></span>
              <span className="dot"></span>
              <span className="dot"></span>
            </div>
          )}
        </div>
        <div className="voice-controls">
          {!isStreaming ? (
            <button className="send-button" onClick={startVoiceConversation}>
              🎙️ Start Voice Chat
            </button>
          ) : (
            <button
              className="send-button stop"
              onClick={stopVoiceConversation}
            >
              ⏹️ Stop
            </button>
          )}
        </div>
      </div>
    </div>
  );
};

export default VoiceConversation;
