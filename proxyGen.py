from io import TextIOWrapper
import os, json, re

try:
	with open("idl.json", "r") as idl:
		config = json.load(idl)

		if not os.path.exists(config['output_path']):
			os.makedirs(config['output_path'])
except FileNotFoundError:
	print("Error: The configuration file 'idl.json' was not found.")
	exit(1)
except json.JSONDecodeError:
	print("Error: There was an issue with decoding the JSON from 'idl.json'.")
	exit(1)
except KeyError as e:
	print(f"Error: Missing expected key in the configuration file: {e}")
	exit(1)

methodsWithImpact = [method for method, methodData in config['methods'].items() if methodData['impact']]
methodsWithoutImpact = [method for method, methodData in config['methods'].items() if not methodData['impact']]

insideMultilineComment = False
SHARDING = "sharding"
PROPAGATE = "propagate"
ALTERNATE = "alternate"
MIXED_PROPAGATE = "MixedPropagate"
DATA_TYPES = ['void', 'int', 'Data', 'Data[]', 'bool']
interactionList = [SHARDING, PROPAGATE, MIXED_PROPAGATE]

def cleanLine(line: str):
	global insideMultilineComment

	if '/*' in line and '*/' in line:
		return re.sub(r'/\*.*?\*/', '', line).strip()

	if '/*' in line:
		insideMultilineComment = True
		return re.sub(r'/\*.*', '', line).strip()

	if insideMultilineComment and '*/' in line:
		insideMultilineComment = False
		return re.sub(r'.*\*/', '', line).strip()

	if insideMultilineComment:
		return

	return re.sub(r'//.*', '', line).strip()

def readInterfaceFile(interfaceFile: TextIOWrapper):
	interfaceFunctions = {}
	interfaceName = ''

	for line_raw in interfaceFile:
		line = cleanLine(line_raw)

		if line:
			wordList = line.split(' ')
			if wordList[0] == 'interface':
				interfaceName = wordList[1]
			elif wordList[0] in DATA_TYPES:
				returnType = wordList[0]
				methodName = wordList[1]

				parameterList = line[line.find('(') + 1:line.rfind(')')].split(',')
				parameterList = [param.strip() for param in parameterList if param.strip()]

				numParam = len([param for param in parameterList if not param.startswith("opt")])

				interfaceFunctions[methodName] = {
					"returnType": returnType,
					"interfaceName": interfaceName,
					"parameterList": parameterList,
					"numParam": numParam
				}

	return interfaceFunctions

def writeHeader(outputFile:TextIOWrapper, interactionMethods: list[str]):
	outputFile.write('''data Param {
	char value[]
}

data Request {
	char functionName[]
	int numParams
	Param params[]
}

data Response {
	// 1 OK - 2 FAILED
	byte status
	// if it's null or "" this has to be translated to null
	char value[]
}

data IPAddr {
	char ip[]
	int port
}

data Int {
	int i
}
''')

	if (SHARDING in interactionMethods):
		outputFile.write("data ShardState {\n")
		outputFile.write("\tInt state[]\n")
		outputFile.write("}\n")

	outputFile.write('''
/* Available list operations */
const char ADD[]          = "add"
const char GET_LENGTH[]   = "getLength"
const char GET_CONTENTS[] = "getContents"
const char CLEAR_LIST[]   = "clearList"

/* IPs */
const char LOCALHOST[] = "localhost"

component provides List:heap(Destructor, AdaptEvents) requires data.json.JSONEncoder parser,
	net.TCPSocket, data.StringUtil strUtil, io.Output out, data.IntUtil iu, ''')

	if (SHARDING in interactionMethods):
		outputFile.write("hash.Multiplicative hash")

	if (PROPAGATE in interactionMethods or ALTERNATE in interactionMethods):
		outputFile.write("net.TCPServerSocket")

	outputFile.write('''
{
	IPAddr remoteDistsIps[] = null
	IPAddr remoteListsIps[] = null
''')

	if (PROPAGATE in interactionMethods or ALTERNATE in interactionMethods):
		outputFile.write("\tMutex remoteListsIpsLock = new Mutex()\n")
		outputFile.write("\tint pointer = 0\n")

	outputFile.write('''
	void setupRemoteDistsIPs() {
		if (remoteDistsIps == null) {
			remoteDistsIps = new IPAddr[2]
			remoteDistsIps[0] = new IPAddr()
			remoteDistsIps[0].ip = new char[](LOCALHOST)
			remoteDistsIps[0].port = 8081
			remoteDistsIps[1] = new IPAddr()
			remoteDistsIps[1].ip = new char[](LOCALHOST)
			remoteDistsIps[1].port = 8082
		}
	}

	void setupRemoteListsIPs() {
		if (remoteListsIps == null) {
			remoteListsIps = new IPAddr[2]
			remoteListsIps[0] = new IPAddr()
			remoteListsIps[0].ip = new char[](LOCALHOST)
			remoteListsIps[0].port = 2010
			remoteListsIps[1] = new IPAddr()
			remoteListsIps[1].ip = new char[](LOCALHOST)
			remoteListsIps[1].port = 2011
		}
	}
''')

	if (PROPAGATE in interactionMethods or ALTERNATE in interactionMethods):
		outputFile.write('''
	void sendMsgToRemoteDists(char msg[]) {
		setupRemoteDistsIPs()
		for (int i = 0; i < remoteDistsIps.arrayLength; i++) {
			connectAndSend(remoteDistsIps[i], msg, true)
		}
	}
	''')

	outputFile.write('''
	Response parseResponse(char content[]) {
		String helper[] = strUtil.explode(content, "!")
		Response response
		if (helper.arrayLength > 1) {
			response = parser.jsonToData(helper[0].string, typeof(Response), null)
			Response response2 = new Response()
			response2.value = helper[1].string
			response2.status = response.status
			response = response2
		} else {
			response = parser.jsonToData(content, typeof(Response), null)
		}
		return response
	}

	Response readResponse(TCPSocket s) {
		Response response = null
		char buf[] = null
		int len = 0
		char command[] = null
		while ((buf = s.recv(1)).arrayLength > 0) {
			command = new char[](command, buf)
			len++
			//stop condition
			if (len >= 4) {
				if ((command[len-4] == "\\r") && (command[len-3] == "\\r") &&
					(command[len-2] == "\\r") && (command[len-1] == "\\r")) {
					response = parseResponse(strUtil.subString(command,
						0, command.arrayLength-4))
					break
				}
			}
		}
		if (response == null) { s.disconnect() }
		return response
	}

	bool establishConnection(IPAddr addr, TCPSocket remoteObj) {
		if (!remoteObj.connect(addr.ip, addr.port)) {
			out.println("Connection error!")
			return false
		}
		return true
	}
''')
	if (PROPAGATE in interactionMethods or ALTERNATE in interactionMethods):
		outputFile.write('''
	Response connectAndSend(IPAddr addr, char content[], bool readResponse) {
		TCPSocket remoteObj = new TCPSocket()
		Response resp = null
		if (establishConnection(addr, remoteObj)) {
			remoteObj.send(content)
			if (readResponse) { resp = readResponse(remoteObj) }
			remoteObj.disconnect()
		}
		return resp
	}
	''')

	if (PROPAGATE in interactionMethods):
		outputFile.write('''
  	void makeGroupRequest(char content[]) {
		setupRemoteListsIPs()
		IPAddr addr = null
		for (int i = 0; i < remoteListsIps.arrayLength; i++) {
		  addr = remoteListsIps[i]
		  asynch::connectAndSend(addr, content, true)
		}
	}
	''')

	if (PROPAGATE in interactionMethods or ALTERNATE in interactionMethods):
		outputFile.write('''
	Response makeRequest(char content[]) {
		setupRemoteListsIPs()
		IPAddr addr = null
		mutex(remoteListsIpsLock) {
			if (remoteListsIps.arrayLength > 1) {
				if (pointer == remoteListsIps.arrayLength) { pointer = 0 }
				addr = remoteListsIps[pointer]
				pointer++
			} else { out.println("ERROR!") }
		}
		return connectAndSend(addr, content, true)
	}

''')
	elif (SHARDING in interactionMethods):
		outputFile.write('''
	Response makeRequestSharding(IPAddr addr, char content[], bool readResponse) {
	TCPSocket remoteObj = new TCPSocket()
		Response resp = null
		if (establishConnection(addr, remoteObj)) {
			remoteObj.send(content)
			if (readResponse) { resp = readResponse(remoteObj) }
			remoteObj.disconnect()
		}
		return resp
	}\n\n''')

def writeFooter(outputFile:TextIOWrapper, interactionMethods: list[str]):
	outputFile.write('''
	void buildFromArray(Data items[]) {
		// TODO
	}

	bool List:clone(Object o) {
		// TODO
		return false
	}

	void clearList() {
		// TODO
	}

	void Destructor:destroy() {
	}

	void AdaptEvents:inactive() {
		if (content != null) {
			content = getContents()
			char msg[] = new char[]("clearList!\\r\\r\\r\\r")
			''')

	if (PROPAGATE in interactionMethods or ALTERNATE in interactionMethods):
		outputFile.write("sendMsgToRemoteDists(msg)")

	elif (SHARDING in interactionMethods):

		outputFile.write('''
			setupRemoteDistsIPs()
			for (int i = 0; i < remoteDistsIps.arrayLength; i++) {
				makeRequestSharding(remoteDistsIps[i], msg, true)
			}''')

	outputFile.write('''
		}
	}

	// this is extremely hardcoded! ):
	void AdaptEvents:active() {
		if (content != null) {''')

	if (PROPAGATE in interactionMethods or ALTERNATE in interactionMethods):
		outputFile.write('''
			char state[] = parser.jsonFromArray(content, null)
			char msg[] = new char[]("../distributor/RemoteList.o!", state, "\\r\\r\\r\\r")
			sendMsgToRemoteDists(msg)''')


	if (SHARDING in interactionMethods):

		outputFile.write('''
			setupRemoteDistsIPs()
			ShardState shardState[] = new ShardState[remoteDistsIps.arrayLength]
			Thread thread[] = new Thread[remoteDistsIps.arrayLength]
			for (int i = 0; i < content.arrayLength; i++) {
				Int num = content[i]
				int remoteIdx = hash.h(num.i, remoteDistsIps.arrayLength)
				if (shardState[remoteIdx] == null) {
					shardState[remoteIdx] = new ShardState()
				}
				shardState[remoteIdx].state = new Int[](shardState[remoteIdx].state, num)
			}
			for (int i = 0; i < remoteDistsIps.arrayLength; i++) {
				char state[] = parser.jsonFromArray(shardState[i].state, null)
				char msg[] = new char[]("../distributor/RemoteList.o!", state, "\\r\\r\\r\\r")
				thread[i] = asynch::makeRequestSharding(remoteDistsIps[i], msg, true)
			}
			for (int i = 0; i < remoteDistsIps.arrayLength; i++) {
				thread[i].join()
			}
			''')
	outputFile.write('''
		}
	}
}''')

def writeFunction(outputFile: TextIOWrapper, interactionMethod: str, interfaceFunctionData: dict, methodName: str, methodData: dict):
	parameterList = interfaceFunctionData['parameterList']
	parameters = ', '.join(parameterList)

	outputFile.write(f"\t{interfaceFunctionData['returnType']} {interfaceFunctionData['interfaceName']}:{methodName} ({parameters}) {{\n")
	outputFile.write("\t\tRequest request = new Request()\n")
	outputFile.write(f'\t\trequest.functionName = "{methodName}"\n')
	outputFile.write(f"\t\trequest.numParams = {interfaceFunctionData['numParam']}\n")
	outputFile.write("\n")
	outputFile.write("\t\tchar requestStr[] = parser.jsonFromData(request, null)\n")

	if methodData['impact']:
		paramName = parameterList[0].split()[-1]

		outputFile.write(f'\t\tchar param[] = parser.jsonFromData({paramName}, null)\n')
		outputFile.write('\t\tchar content2[] = new char[](requestStr, "!", param, "\\r\\r\\r\\r")\n\n')

		if interactionMethod == SHARDING:
			outputFile.write("\t\tsetupRemoteListsIPs()\n")
			outputFile.write(f'\t\tInt num = {paramName}\n')
			outputFile.write('\t\tIPAddr addr = remoteListsIps[hash.h(num.i, remoteListsIps.arrayLength)]\n')
			outputFile.write('\t\tmakeRequestSharding(addr, content2, false)\n')

		elif interactionMethod == ALTERNATE:
			outputFile.write('\t\tmakeRequest(content2)\n\n')

		elif interactionMethod == PROPAGATE:
			outputFile.write('\t\tmakeGroupRequest(content2)\n')

		outputFile.write('\t}\n\n')

	else:
		outputFile.write('\t\tchar content2[] = new char[](requestStr, "!", " ", "\\r\\r\\r\\r")\n')

		if interactionMethod == SHARDING:
			outputFile.write('''\
		setupRemoteListsIPs()
		Int contents[] = null
		for (int i = 0; i < remoteListsIps.arrayLength; i++) {
			Response response = makeRequestSharding(remoteListsIps[i], content2, true)
			Int nums[] = parser.jsonToArray(response.value, typeof(Int[]), null)
			contents = new Int[](contents, nums)
		}
		return contents\n''')

		elif interactionMethod in [PROPAGATE, ALTERNATE]:
			outputFile.write('''\
		Response response = makeRequest(content2)
		Int nums[] = parser.jsonToArray(response.value, typeof(Int[]), null)
		return nums\n''')

		outputFile.write('''\t}\n\n''')

def generateProxyFiles(interfaceFunctions: dict):
	for interaction in interactionList:
		with open(f"{config['output_path']}ListCP{interaction}.dn", "w") as outputFile:
			if interaction == MIXED_PROPAGATE:
				writeHeader(outputFile, [PROPAGATE, ALTERNATE])
				for method in methodsWithImpact:
					methodData = config['methods'][method]
					writeFunction(outputFile, PROPAGATE, interfaceFunctions[method], method, methodData)
				for method in methodsWithoutImpact:
					methodData = config['methods'][method]
					writeFunction(outputFile, ALTERNATE, interfaceFunctions[method], method, methodData)
				writeFooter(outputFile, [PROPAGATE, ALTERNATE])

			else:
				writeHeader(outputFile, [interaction])
				for method in methodsWithImpact + methodsWithoutImpact:
					methodData = config['methods'][method]
					writeFunction(outputFile, interaction, interfaceFunctions[method], method, methodData)
				writeFooter(outputFile, [interaction])

with open(config['interface_path'], 'r') as interfaceFile:
	interfaceFunctions = readInterfaceFile(interfaceFile)
	
generateProxyFiles(interfaceFunctions)
