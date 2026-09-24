import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import Conversation from './Conversation';

const API_URL = 'http://test-api';

// Helpers to build fetch mocks
const makeInitResponse = (overrides = {}) => ({
  ok: true,
  json: () =>
    Promise.resolve({
      bot_config: { humanlike_delay: false, follow_up_on_idle: false },
      initial_utterance: 'Hello from bot!',
      existing_messages: [],
      ...overrides,
    }),
});

const makeAvatarResponse = (imageUrl = null) => ({
  ok: true,
  json: () =>
    Promise.resolve({
      image_url: imageUrl,
      bot_id: '1',
      bot_name: 'TestBot',
      avatar_type: imageUrl ? 'default' : 'none',
    }),
});

const makeChatbotResponse = (text = 'Bot reply.') => ({
  ok: true,
  json: () =>
    Promise.resolve({
      response: text,
      response_chunks: [text],
      humanlike_delay: false,
      chunk_messages: false,
      delay_config: null,
    }),
});

// Stub window.location with the required query params
const stubLocation = (
  search = '?bot_name=TestBot&participant_id=p1&conversation_id=conv1'
) => {
  Object.defineProperty(window, 'location', {
    configurable: true,
    writable: true,
    value: { search, href: `http://localhost/${search}` },
  });
};

const resetLocation = () => {
  Object.defineProperty(window, 'location', {
    configurable: true,
    writable: true,
    value: { search: '', href: 'http://localhost/' },
  });
};

const mockAlert = vi.fn();

beforeEach(() => {
  vi.stubEnv('VITE_API_URL', API_URL);
  mockAlert.mockClear();
  vi.stubGlobal('alert', mockAlert);
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  resetLocation();
});

describe('Conversation — initialization', () => {
  it('does not call fetch when bot_name is missing', async () => {
    stubLocation('?participant_id=p1&conversation_id=conv1');
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    render(<Conversation />);
    // Give effects a tick to settle
    await new Promise(r => setTimeout(r, 50));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('does not call fetch when participant_id is missing', async () => {
    stubLocation('?bot_name=TestBot&conversation_id=conv1');
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    render(<Conversation />);
    await new Promise(r => setTimeout(r, 50));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('calls initialize_conversation on mount with correct body', async () => {
    stubLocation();
    const fetchMock = vi.fn(url => {
      if (url.includes('/initialize_conversation/'))
        return Promise.resolve(makeInitResponse());
      return Promise.resolve(makeAvatarResponse());
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<Conversation />);

    await waitFor(() => {
      const initCall = fetchMock.mock.calls.find(c =>
        c[0].includes('/initialize_conversation/')
      );
      expect(initCall).toBeDefined();
    });

    const [url, opts] = fetchMock.mock.calls.find(c =>
      c[0].includes('/initialize_conversation/')
    );
    expect(url).toContain(`${API_URL}/initialize_conversation/`);
    expect(opts.method).toBe('POST');
    const body = JSON.parse(opts.body);
    expect(body.bot_name).toBe('TestBot');
    expect(body.participant_id).toBe('p1');
    expect(body.conversation_id).toBe('conv1');
  });

  it('shows initial_utterance as the first message', async () => {
    stubLocation();
    vi.stubGlobal(
      'fetch',
      vi.fn(url => {
        if (url.includes('/initialize_conversation/'))
          return Promise.resolve(makeInitResponse());
        return Promise.resolve(makeAvatarResponse());
      })
    );

    render(<Conversation />);

    await waitFor(() => {
      expect(screen.getByText('Hello from bot!')).toBeInTheDocument();
    });
  });

  it('shows existing_messages when returned from init', async () => {
    stubLocation();
    vi.stubGlobal(
      'fetch',
      vi.fn(url => {
        if (url.includes('/initialize_conversation/'))
          return Promise.resolve(
            makeInitResponse({
              initial_utterance: '',
              existing_messages: [
                { sender: 'AI Chatbot', content: 'Prior message' },
                { sender: 'You', content: 'Prior reply' },
              ],
            })
          );
        return Promise.resolve(makeAvatarResponse());
      })
    );

    render(<Conversation />);

    await waitFor(() => {
      expect(screen.getByText('Prior message')).toBeInTheDocument();
      expect(screen.getByText('Prior reply')).toBeInTheDocument();
    });
  });

  it('falls back to default avatar when avatar fetch fails', async () => {
    stubLocation();
    vi.stubGlobal(
      'fetch',
      vi.fn(url => {
        if (url.includes('/initialize_conversation/'))
          return Promise.resolve(makeInitResponse());
        // Avatar fetch fails
        return Promise.resolve({
          ok: false,
          json: () => Promise.resolve({ error: 'Not found' }),
        });
      })
    );

    render(<Conversation />);

    await waitFor(() => {
      expect(screen.getByText('Hello from bot!')).toBeInTheDocument();
    });
    // No avatar image should appear (image_url is null in fallback)
    expect(screen.queryByAltText('Avatar')).not.toBeInTheDocument();
  });
});

describe('Conversation — handleSubmit', () => {
  beforeEach(() => {
    stubLocation();
  });

  const renderWithInit = async (chatResponse = makeChatbotResponse()) => {
    vi.stubGlobal(
      'fetch',
      vi.fn(url => {
        if (url.includes('/initialize_conversation/'))
          return Promise.resolve(makeInitResponse());
        if (url.includes('/avatar/'))
          return Promise.resolve(makeAvatarResponse());
        if (url.includes('/chatbot/')) return Promise.resolve(chatResponse);
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
      })
    );

    render(<Conversation />);

    await waitFor(() => {
      expect(screen.getByText('Hello from bot!')).toBeInTheDocument();
    });
  };

  it('shows alert and does not submit when message is empty', async () => {
    await renderWithInit();
    fireEvent.submit(document.querySelector('form'));
    expect(mockAlert).toHaveBeenCalledWith('Please enter a message.');
  });

  it('adds user message to the list immediately', async () => {
    await renderWithInit();
    const input = screen.getByPlaceholderText(/type your message/i);
    fireEvent.change(input, { target: { value: 'My question' } });
    fireEvent.submit(input.closest('form'));

    expect(screen.getByText('My question')).toBeInTheDocument();
  });

  it('calls /api/chatbot/ with the user message', async () => {
    const fetchMock = vi.fn(url => {
      if (url.includes('/initialize_conversation/'))
        return Promise.resolve(makeInitResponse());
      if (url.includes('/avatar/'))
        return Promise.resolve(makeAvatarResponse());
      if (url.includes('/chatbot/'))
        return Promise.resolve(makeChatbotResponse());
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<Conversation />);
    await waitFor(() => screen.getByText('Hello from bot!'));

    const input = screen.getByPlaceholderText(/type your message/i);
    fireEvent.change(input, { target: { value: 'Test message' } });
    fireEvent.submit(input.closest('form'));

    await waitFor(() => {
      const chatCall = fetchMock.mock.calls.find(c =>
        c[0].includes('/chatbot/')
      );
      expect(chatCall).toBeDefined();
      const body = JSON.parse(chatCall[1].body);
      expect(body.message).toBe('Test message');
    });
  });

  it('shows bot reply after chatbot API response', async () => {
    await renderWithInit(makeChatbotResponse('Smart answer.'));

    const input = screen.getByPlaceholderText(/type your message/i);
    fireEvent.change(input, { target: { value: 'Question?' } });
    fireEvent.submit(input.closest('form'));

    await waitFor(() => {
      expect(screen.getByText('Smart answer.')).toBeInTheDocument();
    });
  });

  it('clears the input after submit', async () => {
    await renderWithInit();

    const input = screen.getByPlaceholderText(/type your message/i);
    fireEvent.change(input, { target: { value: 'Hi' } });
    fireEvent.submit(input.closest('form'));

    expect(input.value).toBe('');
  });

  it('alerts on non-ok chatbot response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(url => {
        if (url.includes('/initialize_conversation/'))
          return Promise.resolve(makeInitResponse());
        if (url.includes('/avatar/'))
          return Promise.resolve(makeAvatarResponse());
        if (url.includes('/chatbot/'))
          return Promise.resolve({
            ok: false,
            json: () => Promise.resolve({ error: 'Server error' }),
          });
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
      })
    );

    render(<Conversation />);
    await waitFor(() => screen.getByText('Hello from bot!'));

    const input = screen.getByPlaceholderText(/type your message/i);
    fireEvent.change(input, { target: { value: 'Hi' } });
    fireEvent.submit(input.closest('form'));

    await waitFor(() => {
      expect(mockAlert).toHaveBeenCalledWith(
        expect.stringContaining('Server error')
      );
    });
  });
});

describe('Conversation — failures are reported, not swallowed', () => {
  // These paths were silent: a failed init produced a blank chat with nothing
  // in the console, which is indistinguishable from the script never running.
  // Diagnosing a mis-wired survey depends on the browser saying something.
  let errorSpy;

  beforeEach(() => {
    errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    errorSpy.mockRestore();
  });

  it('reports an initialization failure rejected by the server', async () => {
    stubLocation();
    global.fetch = vi.fn(() =>
      Promise.resolve({
        ok: false,
        json: () => Promise.resolve({ error: 'No bot found' }),
      })
    );

    render(<Conversation />);

    await waitFor(() => expect(errorSpy).toHaveBeenCalled());
    expect(errorSpy.mock.calls.flat().join(' ')).toMatch(/initialize/i);
  });

  it('reports an initialization failure caused by the network', async () => {
    stubLocation();
    global.fetch = vi.fn(() => Promise.reject(new Error('network down')));

    render(<Conversation />);

    await waitFor(() => expect(errorSpy).toHaveBeenCalled());
  });

  it('reports a failure to send a message', async () => {
    stubLocation();
    global.fetch = vi
      .fn()
      .mockResolvedValueOnce(makeInitResponse())
      .mockResolvedValueOnce(makeAvatarResponse())
      .mockRejectedValueOnce(new Error('network down'));

    render(<Conversation />);
    await screen.findByText('Hello from bot!');

    fireEvent.change(screen.getByPlaceholderText(/type your message/i), {
      target: { value: 'hi' },
    });
    fireEvent.click(screen.getByText(/send/i));

    await waitFor(() => expect(errorSpy).toHaveBeenCalled());
  });

  it('reports missing URL parameters rather than rendering an empty chat', async () => {
    // The exact failure from a live Qualtrics survey: participant_id piped
    // empty, init returned early, and the page sat blank with no explanation.
    stubLocation('?bot_name=TestBot&conversation_id=conv1');
    global.fetch = vi.fn();

    render(<Conversation />);

    await waitFor(() => expect(errorSpy).toHaveBeenCalled());
    expect(errorSpy.mock.calls.flat().join(' ')).toMatch(/participant_id/);
    expect(global.fetch).not.toHaveBeenCalled();
  });
});
