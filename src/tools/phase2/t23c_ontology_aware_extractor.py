"""
T23c: Ontology-Aware Entity Extractor
Replaces generic spaCy NER with domain-specific extraction using LLMs and ontologies.
"""

import os
import json
import logging
import uuid
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, asdict
import numpy as np
from datetime import datetime

import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
from openai import OpenAI

from src.core.identity_service import Entity, Relationship, Mention
from src.core.identity_service import IdentityService
from src.ontology_generator import DomainOntology, EntityType, RelationshipType

logger = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    """Result of ontology-aware extraction."""
    entities: List[Entity]
    relationships: List[Relationship]
    mentions: List[Mention]
    extraction_metadata: Dict[str, Any]


class OntologyAwareExtractor:
    """
    Extract entities and relationships using domain-specific ontologies.
    Uses Gemini for extraction and OpenAI for embeddings.
    """
    
    def __init__(self, 
                 identity_service: IdentityService,
                 google_api_key: Optional[str] = None,
                 openai_api_key: Optional[str] = None):
        """
        Initialize the extractor.
        
        Args:
            identity_service: Service for entity resolution and identity management
            google_api_key: Google API key for Gemini
            openai_api_key: OpenAI API key for embeddings
        """
        self.identity_service = identity_service
        
        # Initialize Gemini
        self.google_api_key = (google_api_key or 
                              os.getenv("GOOGLE_API_KEY") or 
                              os.getenv("GOOGLE_AI_STUDIO_KEY"))
        if not self.google_api_key:
            raise ValueError("Google API key required")
        
        genai.configure(api_key=self.google_api_key)
        # IMPORTANT: DO NOT CHANGE THIS MODEL - gemini-2.5-flash has 1000 RPM limit
        # Other models have much lower limits (e.g., 10 RPM) and will cause quota errors
        self.gemini_model = genai.GenerativeModel('gemini-2.5-flash')
        
        # Safety settings for academic content
        self.safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }
        
        # Initialize OpenAI for embeddings
        self.openai_api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        if self.openai_api_key:
            self.openai_client = OpenAI(api_key=self.openai_api_key)
        else:
            logger.warning("OpenAI API key not provided. Embeddings will use mock values.")
            self.openai_client = None
    
    def extract_entities(self, 
                        text: str, 
                        ontology: DomainOntology,
                        source_ref: str,
                        confidence_threshold: float = 0.7,
                        use_mock_apis: bool = False) -> ExtractionResult:
        """
        Extract entities and relationships from text using domain ontology.
        
        Args:
            text: Text to extract from
            ontology: Domain-specific ontology
            source_ref: Reference to source document
            confidence_threshold: Minimum confidence for extraction
            
        Returns:
            ExtractionResult with entities, relationships, and mentions
        """
        start_time = datetime.now()
        
        # Step 1: Use OpenAI to extract based on ontology (or mock if requested)
        if use_mock_apis:
            raw_extraction = self._mock_extract(text, ontology)
        else:
            # Use OpenAI instead of Gemini to avoid safety filter issues
            raw_extraction = self._openai_extract(text, ontology)
        
        # Step 2: Create mentions and entities
        entities = []
        mentions = []
        entity_map = {}  # Track text -> entity mapping
        
        for raw_entity in raw_extraction.get("entities", []):
            if raw_entity.get("confidence", 0) < confidence_threshold:
                continue
            
            # Create mention
            mention = self._create_mention(
                surface_text=raw_entity["text"],
                entity_type=raw_entity["type"],
                source_ref=source_ref,
                confidence=raw_entity.get("confidence", 0.8),
                context=raw_entity.get("context", "")
            )
            mentions.append(mention)
            
            # Create or resolve entity
            entity = self._resolve_or_create_entity(
                surface_text=raw_entity["text"],
                entity_type=raw_entity["type"],
                ontology=ontology,
                confidence=raw_entity.get("confidence", 0.8)
            )
            entities.append(entity)
            entity_map[raw_entity["text"]] = entity
            
            # Link mention to entity
            self.identity_service.link_mention_to_entity(mention.id, entity.id)
        
        # Step 3: Create relationships
        relationships = []
        for raw_rel in raw_extraction.get("relationships", []):
            if raw_rel.get("confidence", 0) < confidence_threshold:
                continue
            
            source_entity = entity_map.get(raw_rel["source"])
            target_entity = entity_map.get(raw_rel["target"])
            
            if source_entity and target_entity:
                relationship = Relationship(
                    id=f"rel_{len(relationships)}_{source_ref}",
                    source_id=source_entity.id,
                    target_id=target_entity.id,
                    relationship_type=raw_rel["relation"],
                    confidence=raw_rel.get("confidence", 0.8),
                    attributes={
                        "extracted_from": source_ref,
                        "context": raw_rel.get("context", ""),
                        "ontology_domain": ontology.domain_name
                    }
                )
                relationships.append(relationship)
        
        # Step 4: Generate embeddings for entities
        if self.openai_client:
            self._generate_embeddings(entities, ontology)
        
        extraction_time = (datetime.now() - start_time).total_seconds()
        
        return ExtractionResult(
            entities=entities,
            relationships=relationships,
            mentions=mentions,
            extraction_metadata={
                "ontology_domain": ontology.domain_name,
                "extraction_time_seconds": extraction_time,
                "source_ref": source_ref,
                "total_entities": len(entities),
                "total_relationships": len(relationships),
                "confidence_threshold": confidence_threshold
            }
        )
    
    def _mock_extract(self, text: str, ontology: DomainOntology) -> Dict[str, Any]:
        """Generate mock extraction results for testing purposes."""
        logger.info(f"Using mock extraction for text length: {len(text)}")
        logger.info(f"Ontology domain: {ontology.domain_name}")
        
        # Create mock entities based on simple text analysis and ontology
        mock_entities = []
        mock_relationships = []
        
        # Extract potential entity names using simple heuristics
        words = text.split()
        capitalized_words = [w for w in words if w[0].isupper() and len(w) > 2]
        
        # Map to ontology entity types
        for i, word in enumerate(capitalized_words[:5]):  # Limit to 5 entities
            if i < len(ontology.entity_types):
                entity_type = ontology.entity_types[i]
                mock_entities.append({
                    "text": word,
                    "type": entity_type.name,
                    "confidence": 0.85,
                    "context": f"Mock entity extracted from text"
                })
        
        # Create mock relationships between consecutive entities
        for i in range(len(mock_entities) - 1):
            if i < len(ontology.relationship_types):
                rel_type = ontology.relationship_types[i]
                mock_relationships.append({
                    "source": mock_entities[i]["text"],
                    "target": mock_entities[i + 1]["text"],
                    "relation": rel_type.name,
                    "confidence": 0.8
                })
        
        logger.info(f"Mock extraction: {len(mock_entities)} entities, {len(mock_relationships)} relationships")
        
        return {
            "entities": mock_entities,
            "relationships": mock_relationships,
            "extraction_metadata": {
                "method": "mock",
                "ontology_domain": ontology.domain_name,
                "text_length": len(text)
            }
        }
    
    def _gemini_extract(self, text: str, ontology: DomainOntology) -> Dict[str, Any]:
        """Use Gemini to extract entities and relationships based on ontology."""
        logger.info(f"_gemini_extract called with text length: {len(text)}")
        logger.info(f"Ontology domain: {ontology.domain_name}")
        
        # Build entity and relationship descriptions
        entity_desc = []
        for et in ontology.entity_types:
            examples = ", ".join(et.examples[:3]) if et.examples else "no examples"
            entity_desc.append(f"- {et.name}: {et.description} (examples: {examples})")
        
        logger.info(f"Entity types: {len(entity_desc)}")
        
        rel_desc = []
        for rt in ontology.relationship_types:
            rel_desc.append(f"- {rt.name}: {rt.description} (connects {rt.source_types} to {rt.target_types})")
        
        logger.info(f"Relationship types: {len(rel_desc)}")
        
        guidelines = "\n".join(f"- {g}" for g in ontology.extraction_patterns)
        
        prompt = f"""Extract domain-specific entities and relationships from the following text using the provided ontology.

DOMAIN: {ontology.domain_name}
{ontology.domain_description}

ENTITY TYPES:
{chr(10).join(entity_desc)}

RELATIONSHIP TYPES:
{chr(10).join(rel_desc)}

EXTRACTION GUIDELINES:
{guidelines}

TEXT TO ANALYZE:
{text}

NOTE: Only extract entities and relationships that match the ontology types above. If the text doesn't contain any entities matching the defined types, return empty arrays.

Extract entities and relationships in this JSON format:
{{
    "entities": [
        {{
            "text": "exact text from source",
            "type": "ENTITY_TYPE_NAME",
            "confidence": 0.95,
            "context": "surrounding context"
        }}
    ],
    "relationships": [
        {{
            "source": "source entity text",
            "relation": "RELATIONSHIP_TYPE",
            "target": "target entity text",
            "confidence": 0.90,
            "context": "relationship context"
        }}
    ]
}}

Important:
1. Only extract entities that match the defined types
2. Use exact text from the source
3. Include confidence scores (0-1)
4. Provide context for disambiguation
5. Only extract relationships between identified entities

Respond ONLY with the JSON."""
        
        logger.info(f"Sending prompt to Gemini (first 500 chars): {prompt[:500]}...")
        
        try:
            response = self.gemini_model.generate_content(
                prompt,
                generation_config=genai.types.GenerationConfig(
                    temperature=0.3,  # Low temperature for consistent extraction
                    candidate_count=1,
                    max_output_tokens=4000,
                ),
                safety_settings=self.safety_settings
            )
            
            # Parse response - handle safety filter blocks
            try:
                cleaned = response.text.strip()
                logger.info(f"Gemini raw response (first 500 chars): {cleaned[:500]}...")
                
                if cleaned.startswith("```json"):
                    cleaned = cleaned[7:]
                if cleaned.startswith("```"):
                    cleaned = cleaned[3:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
                
                result = json.loads(cleaned)
                logger.info(f"Gemini extraction successful: {len(result.get('entities', []))} entities, {len(result.get('relationships', []))} relationships")
                return result
            except Exception as parse_error:
                # Response parsing failed - likely safety filter block
                logger.warning(f"Failed to parse Gemini response: {parse_error}")
                logger.warning(f"Response text was: {response.text[:500]}...")
                raise Exception(f"Gemini response parsing failed: {parse_error}")
            
        except Exception as e:
            logger.error(f"Gemini extraction failed: {e}")
            # Fallback to simple pattern-based extraction for testing
            return self._fallback_pattern_extraction(text, ontology)
    
    def _openai_extract(self, text: str, ontology: DomainOntology) -> Dict[str, Any]:
        """Use OpenAI to extract entities and relationships based on ontology."""
        logger.info(f"_openai_extract called with text length: {len(text)}")
        logger.info(f"Ontology domain: {ontology.domain_name}")
        
        if not self.openai_client:
            logger.warning("OpenAI client not available, falling back to pattern extraction")
            return self._fallback_pattern_extraction(text, ontology)
        
        # Build entity and relationship descriptions (same as Gemini)
        entity_desc = []
        for et in ontology.entity_types:
            examples = ", ".join(et.examples[:3]) if et.examples else "no examples"
            entity_desc.append(f"- {et.name}: {et.description} (examples: {examples})")
        
        rel_desc = []
        for rt in ontology.relationship_types:
            rel_desc.append(f"- {rt.name}: {rt.description} (connects {rt.source_types} to {rt.target_types})")
        
        # Build prompt (same as Gemini but formatted for OpenAI)
        prompt = f"""Extract entities and relationships from the following text using the domain ontology.

**Domain:** {ontology.domain_name}

**Entity Types:**
{chr(10).join(entity_desc)}

**Relationship Types:**
{chr(10).join(rel_desc)}

**Text to analyze:**
{text}

**Instructions:**
1. Identify entities that match the defined types
2. Find relationships between entities
3. Return confidence scores (0.0-1.0)
4. Include context for each extraction

**Response format (JSON only):**
{{
    "entities": [
        {{"text": "entity text", "type": "EntityType", "confidence": 0.9, "context": "surrounding text"}}
    ],
    "relationships": [
        {{"source": "entity1", "target": "entity2", "relation": "RelationType", "confidence": 0.8, "context": "context"}}
    ]
}}

Respond ONLY with valid JSON."""
        
        logger.info(f"Sending prompt to OpenAI (first 500 chars): {prompt[:500]}...")
        
        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,  # Low temperature for consistent extraction
                max_tokens=4000,
            )
            
            # Parse response
            try:
                cleaned = response.choices[0].message.content.strip()
                logger.info(f"OpenAI raw response (first 500 chars): {cleaned[:500]}...")
                
                # Clean JSON formatting
                if cleaned.startswith("```json"):
                    cleaned = cleaned[7:]
                if cleaned.startswith("```"):
                    cleaned = cleaned[3:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
                
                result = json.loads(cleaned)
                logger.info(f"OpenAI extraction successful: {len(result.get('entities', []))} entities, {len(result.get('relationships', []))} relationships")
                return result
                
            except Exception as parse_error:
                logger.warning(f"Failed to parse OpenAI response: {parse_error}")
                logger.warning(f"Response text was: {cleaned[:500]}...")
                raise Exception(f"OpenAI response parsing failed: {parse_error}")
            
        except Exception as e:
            logger.error(f"OpenAI extraction failed: {e}")
            # Fallback to pattern-based extraction
            return self._fallback_pattern_extraction(text, ontology)
    
    def _fallback_pattern_extraction(self, text: str, ontology: DomainOntology) -> Dict[str, Any]:
        """Fallback pattern-based extraction when Gemini fails."""
        import re
        
        entities = []
        relationships = []
        
        # Simple pattern matching for common entity types
        patterns = {
            "PERSON": [
                r"Dr\.\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
                r"Professor\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
                r"Prof\.\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
            ],
            "ORGANIZATION": [
                r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+University",
                r"University\s+of\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
                r"([A-Z][A-Z]+)",  # Acronyms
            ],
            "RESEARCH_TOPIC": [
                r"research\s+on\s+([a-z\s]+)",
                r"study\s+of\s+([a-z\s]+)",
                r"([a-z\s]+)\s+research",
            ]
        }
        
        entity_texts = set()  # Avoid duplicates
        
        for entity_type, pattern_list in patterns.items():
            for pattern in pattern_list:
                matches = re.finditer(pattern, text, re.IGNORECASE)
                for match in matches:
                    entity_text = match.group(1).strip()
                    if len(entity_text) > 2 and entity_text not in entity_texts:
                        entity_texts.add(entity_text)
                        entities.append({
                            "text": entity_text,
                            "type": entity_type,
                            "confidence": 0.8,
                            "context": match.group(0)
                        })
        
        # Simple relationship patterns
        rel_patterns = [
            (r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+at\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)", "AFFILIATED_WITH"),
            (r"research\s+by\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)", "CONDUCTED_BY"),
        ]
        
        for pattern, relation_type in rel_patterns:
            matches = re.finditer(pattern, text, re.IGNORECASE)
            for match in matches:
                if len(match.groups()) >= 2:
                    relationships.append({
                        "source": match.group(1).strip(),
                        "relation": relation_type,
                        "target": match.group(2).strip(),
                        "confidence": 0.7,
                        "context": match.group(0)
                    })
        
        return {"entities": entities, "relationships": relationships}
    
    def _create_mention(self, surface_text: str, entity_type: str, 
                       source_ref: str, confidence: float, context: str) -> Mention:
        """Create a mention for the extracted text."""
        mention_data = self.identity_service.create_mention(
            surface_form=surface_text,
            start_pos=0,  # Would need proper position tracking in production
            end_pos=len(surface_text),
            source_ref=source_ref,
            entity_type=entity_type,
            confidence=confidence
        )
        
        return Mention(
            id=mention_data.get("mention_id", f"men_{uuid.uuid4().hex[:8]}"),
            surface_form=surface_text,
            normalized_form=surface_text.strip(),
            start_pos=0,
            end_pos=len(surface_text),
            source_ref=source_ref,
            confidence=confidence,
            entity_type=entity_type,
            context=context
        )
    
    def _resolve_or_create_entity(self, surface_text: str, entity_type: str,
                                 ontology: DomainOntology, confidence: float) -> Entity:
        """Resolve to existing entity or create new one."""
        # Use the find_or_create_entity method which combines both operations
        entity_data = self.identity_service.find_or_create_entity(
            mention_text=surface_text,
            entity_type=entity_type,
            context=f"Ontology: {ontology.domain_name}",
            confidence=confidence
        )
        
        # Determine if this was resolved from existing entity
        is_resolved = entity_data.get("action") == "found"
        
        return Entity(
            id=entity_data["entity_id"],
            canonical_name=entity_data["canonical_name"],
            entity_type=entity_type,
            confidence=confidence,
            attributes={
                "ontology_domain": ontology.domain_name,
                "resolved": is_resolved,
                "similarity_score": entity_data.get("similarity_score", 1.0)
            }
        )
    
    def _generate_embeddings(self, entities: List[Entity], ontology: DomainOntology):
        """Generate contextual embeddings for entities using OpenAI."""
        for entity in entities:
            # Create context-rich description
            entity_type_info = next((et for et in ontology.entity_types 
                                   if et.name == entity.entity_type), None)
            
            if entity_type_info:
                context = f"{entity.entity_type}: {entity.canonical_name} - {entity_type_info.description}"
            else:
                context = f"{entity.entity_type}: {entity.canonical_name}"
            
            try:
                # Generate embedding
                if self.openai_client:
                    response = self.openai_client.embeddings.create(
                        model="text-embedding-ada-002",
                        input=context
                    )
                    embedding = response.data[0].embedding
                else:
                    # No OpenAI client available
                    raise Exception("OpenAI client not available")
                
                # Store embedding (would go to Qdrant in production)
                entity.attributes["embedding"] = embedding
                entity.attributes["embedding_model"] = "text-embedding-ada-002"
                entity.attributes["embedding_context"] = context
                
            except Exception as e:
                logger.error(f"Failed to generate embedding for {entity.canonical_name}: {e}")
                # Use mock embedding
                entity.attributes["embedding"] = np.random.randn(1536).tolist()
                entity.attributes["embedding_model"] = "mock"
    
    def batch_extract(self, 
                     texts: List[Tuple[str, str]],  # (text, source_ref) pairs
                     ontology: DomainOntology,
                     confidence_threshold: float = 0.7) -> List[ExtractionResult]:
        """
        Extract from multiple texts efficiently.
        
        Args:
            texts: List of (text, source_ref) tuples
            ontology: Domain ontology to use
            confidence_threshold: Minimum confidence
            
        Returns:
            List of ExtractionResult objects
        """
        results = []
        
        for text, source_ref in texts:
            try:
                result = self.extract_entities(
                    text=text,
                    ontology=ontology,
                    source_ref=source_ref,
                    confidence_threshold=confidence_threshold
                )
                results.append(result)
            except Exception as e:
                logger.error(f"Failed to extract from {source_ref}: {e}")
                # Return empty result on failure
                results.append(ExtractionResult(
                    entities=[],
                    relationships=[],
                    mentions=[],
                    extraction_metadata={
                        "error": str(e),
                        "source_ref": source_ref
                    }
                ))
        
        return results